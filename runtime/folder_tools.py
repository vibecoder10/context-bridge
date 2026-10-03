"""Bounded, on-demand folder reads shared by Claude and Codex."""
import json
import base64
import hashlib
import re
import os
from pathlib import Path
import time
import uuid

from folder_access import FolderPolicy, MEDIA
from relay import BridgeError
from image_content import image_result

READ_TOOLS = [
    {'name':'bridge_list_files','description':'Browse the approved folder on demand. Start at . then choose a project subfolder. Direct children by default; optional recursion. Follow next_cursor for another bounded page. Private, ignored and symlink paths are excluded.',
     'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'recursive':{'type':'boolean'},'cursor':{'type':'string'}},'additionalProperties':False}},
    {'name':'bridge_search_files','description':'Search UTF-8 text for a literal phrase inside an approved subfolder. Prefer a specific project over the whole vault. Follow next_cursor until done; each call scans a bounded page. Returns relative paths, line numbers and short matching excerpts.',
     'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'query':{'type':'string'},'case_sensitive':{'type':'boolean'},'cursor':{'type':'string'}},'required':['query'],'additionalProperties':False}},
    {'name':'bridge_read_file','description':'Read one approved UTF-8 file in bounded chunks. Use relative paths found by bridge_list_files or bridge_search_files. Follow next_offset when has_more is true. Never read blocked/private files.',
     'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':32000}},'required':['path'],'additionalProperties':False}},
    {'name':'bridge_view_image','description':'View actual pixels of one PNG, JPG, GIF or WebP in your approved folder. Use this before visual feedback; text reading cannot view images. Existing private/ignore/symlink boundaries and 10 MiB limit apply.',
     'inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}},
    {'name':'bridge_view_chat_image','description':'View actual pixels of one image already shared in this chat. Use an artifact_id from a chat post images list. Authenticated access is limited to this exact connected project. Never fetch an arbitrary URL.',
     'inputSchema':{'type':'object','properties':{'artifact_id':{'type':'string'}},'required':['artifact_id'],'additionalProperties':False}},
]
PAGE_ENTRIES = 5000
PAGE_SECONDS = 3

class FolderReader:
    def __init__(self, folder, config_path=None):
        self.policy = FolderPolicy(folder)
        self.config_path = Path(config_path) if config_path else None
        self.cursors = {}
        self.sources = []

    def fresh(self):
        if self.config_path:
            config = json.loads(self.config_path.read_text())
            if config.get('folder') != str(self.policy.root):
                raise BridgeError('The folder grant changed. Reconnect the listener.')
        if FolderPolicy(self.policy.root).fingerprint != self.policy.fingerprint:
            raise BridgeError('The folder policy changed. Reconnect the listener.')

    def target(self, value, directory=False):
        if not isinstance(value,str) or not value or any(c in value for c in ('\\','\x00','\r','\n')):
            raise BridgeError('Use a relative path inside the approved folder.')
        if value == '.' and directory:
            return self.policy.root
        rel = Path(value)
        if rel.is_absolute() or any(p in ('','.','..') for p in value.split('/')):
            raise BridgeError('Use a relative path inside the approved folder.')
        path = self.policy.root / rel
        for component in [path,*path.parents]:
            if component == self.policy.root:break
            if component.is_symlink():raise BridgeError('Reads through symbolic links are blocked.')
        return self.policy.path(path,directory=directory)

    def record(self, names):
        names = list(dict.fromkeys(names))
        self.sources.extend(names)
        if names and self.config_path:
            audit = self.config_path.parent / 'read-log.jsonl'
            fd = os.open(audit,os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
            with os.fdopen(fd,'a') as stream:
                stream.write(json.dumps({'paths':names})+'\n')

    def walk(self, start, recursive):
        # Yield even denied entries so the scan budget includes them. Never read
        # file contents just to discover paths; no full-vault inventory required.
        pending = [start]
        while pending:
            directory = pending.pop()
            try:
                self.target(directory.relative_to(self.policy.root).as_posix(),directory=True)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = Path(entry.path);relative = path.relative_to(self.policy.root).as_posix()
                        try:
                            isdir = entry.is_dir(follow_symlinks=False)
                            if entry.is_symlink() or self.policy.denied(relative,isdir):
                                yield None;continue
                            if isdir:
                                if recursive:pending.append(path)
                                yield {'path':relative,'type':'folder'}
                            elif entry.is_file(follow_symlinks=False):
                                yield {'path':relative,'type':'file'}
                            else:yield None
                        except OSError:yield None
            except (OSError,BridgeError):yield None

    def search(self, start, query, sensitive):
        needle = query if sensitive else query.casefold()
        for item in self.walk(start,True):
            yield None
            if not item or item['type'] != 'file':continue
            try:
                path = self.target(item['path'])
                if path.suffix.lower() in MEDIA:continue
                for number,line in enumerate(path.read_text().splitlines(),1):
                    candidate = line if sensitive else line.casefold()
                    index = candidate.find(needle)
                    if index >= 0:
                        # Avoid returning an entire large/minified line.
                        yield {'path':item['path'],'line':number,'excerpt':line[max(0,index-120):index+len(query)+200][:600]}
                    else:yield None
            except (OSError,BridgeError,UnicodeError):continue

    def page(self, name, args):
        path = args.get('path','.')
        key = (name,path,args.get('recursive',False),args.get('query'),args.get('case_sensitive',False))
        cursor = args.get('cursor')
        if cursor:
            saved = self.cursors.pop(cursor,None)
            if not saved:raise BridgeError('This cursor expired. Start a new browse or search.')
            if saved[0] != key:
                self.cursors[cursor] = saved
                raise BridgeError('Keep the same path and search options when continuing a page.')
            iterator = saved[1]
        else:
            start = self.target(path,directory=True)
            if name == 'bridge_search_files':
                query = args.get('query')
                if not isinstance(query,str) or not query or len(query)>200:
                    raise BridgeError('Use a literal search phrase of 1 to 200 characters.')
                iterator = self.search(start,query,args.get('case_sensitive') is True)
            else:iterator = self.walk(start,args.get('recursive') is True)
        result=[];deadline=time.monotonic()+PAGE_SECONDS;scanned=0;done=False
        limit=50 if name=='bridge_search_files' else 100
        while scanned<PAGE_ENTRIES and len(result)<limit and time.monotonic()<deadline:
            try:item=next(iterator)
            except StopIteration:done=True;break
            scanned+=1
            if item:result.append(item)
        self.fresh()
        token=None
        if not done:
            while len(self.cursors)>=8:
                _,old=self.cursors.pop(next(iter(self.cursors)));old.close()
            token=uuid.uuid4().hex;self.cursors[token]=(key,iterator)
        if name=='bridge_search_files':self.record([item['path'] for item in result])
        return {'entries' if name=='bridge_list_files' else 'matches':result,'has_more':not done,'next_cursor':token,'scanned':scanned}

    def run(self, name, args):
        self.fresh()
        if name in ('bridge_list_files','bridge_search_files'):return self.page(name,args)
        if name=='bridge_view_chat_image':
            artifact_id=args.get('artifact_id')
            if not self.config_path or not isinstance(artifact_id,str) or not re.fullmatch(r'[a-f0-9-]{36}',artifact_id):
                raise BridgeError('Choose an image artifact ID from this chat.')
            from client import Client
            client=Client(self.config_path)
            download=client.request('get_artifact',artifact_id=artifact_id)
            artifact=download['artifact'];chunks=download['chunks']
            if artifact.get('project')!=client.config['project_id'] or artifact.get('id')!=artifact_id or not artifact.get('ready'):
                raise BridgeError('This image is unavailable in this project.')
            size=artifact.get('size')
            if type(size) is not int or not 0<size<=10*1024*1024 or len(chunks)>64 or sum(len(c) for c in chunks)>14*1024*1024:
                raise BridgeError('Shared image exceeds the read size limit.')
            data=b''.join(base64.b64decode(c,validate=True) for c in chunks)
            digest=hashlib.sha256(data).hexdigest()
            if len(data)!=size or digest!=artifact.get('sha256'):
                raise BridgeError('Shared image checksum mismatch.')
            self.fresh()
            result=image_result(data,{'artifact_id':artifact_id,'name':artifact['name'],'sha256':digest,'viewed':True},artifact.get('mime'))
            fd=os.open(str(self.config_path.parent/'image-read-log.jsonl'),os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
            with os.fdopen(fd,'a') as log:log.write(json.dumps({'artifact_id':artifact_id,'sha256':digest})+'\n')
            return result
        if name=='bridge_view_image':
            path=self.target(args.get('path'))
            if path.suffix.lower() not in ('.png','.jpg','.jpeg','.gif','.webp'):
                raise BridgeError('Choose a supported image file.')
            data=path.read_bytes();self.fresh();self.policy.path(path)
            relative=path.relative_to(self.policy.root).as_posix()
            mime={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.webp':'image/webp'}[path.suffix.lower()]
            result=image_result(data,{'path':relative,'sha256':hashlib.sha256(data).hexdigest(),'viewed':True},mime)
            self.record([relative]);return result
        if name != 'bridge_read_file':raise BridgeError('Unknown folder read tool.')
        path = self.target(args.get('path'))
        if path.suffix.lower() in MEDIA:raise BridgeError('Use the local image/PDF input for media. This tool reads text.')
        offset,limit=args.get('offset',0),args.get('limit',16000)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=32000:
            raise BridgeError('Use a nonnegative offset and a limit of 1 to 32000 characters.')
        contents=path.read_text();chunk=contents[offset:offset+limit];following=offset+len(chunk)
        self.fresh();relative=path.relative_to(self.policy.root).as_posix();self.record([relative])
        return {'path':relative,'text':chunk,'has_more':following<len(contents),'next_offset':following if following<len(contents) else None}

    def close(self):
        for _,iterator in self.cursors.values():iterator.close()
        self.cursors.clear()
