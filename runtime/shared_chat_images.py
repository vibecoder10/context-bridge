"""Read only the actual published PNGs attached to this leased shared job."""
import base64
import hashlib
from pathlib import Path
from routine_worker import png

def shared_ids(work):
 return {a['id'] for post in work.get('posts',[]) for a in post.get('images',[])}
def shared_image(agent,work,artifact):
 if artifact not in shared_ids(work):raise ValueError('Use an image attached to this shared job.')
 received=agent.request('get_artifact',artifact_id=artifact)
 meta,chunks=received['artifact'],received['chunks']
 if meta.get('id')!=artifact or meta.get('project')!=agent.config['project_id'] or not meta.get('ready') or not 0<meta.get('size',0)<=10*1024*1024 or len(chunks)>512 or sum(map(len,chunks))>14*1024*1024:raise ValueError('Invalid shared image.')
 data=png(b''.join(base64.b64decode(c,validate=True) for c in chunks))
 if len(data)!=meta['size'] or hashlib.sha256(data).hexdigest()!=meta['sha256']:raise ValueError('Shared image checksum mismatch.')
 return data,meta
def download_shared_images(agent,work,directory):
 directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
 available=list(dict.fromkeys(a['id'] for post in work.get('posts',[]) for a in post.get('images',[])))
 question=work['job'].get('question','');selected=[a for a in available if a in question] or available[-1:]
 paths=[]
 for aid in selected[:3]:
  data,meta=shared_image(agent,work,aid)
  if not __import__('re').fullmatch(r'[a-f0-9-]{36}',aid):raise ValueError('Invalid shared artifact ID.')
  path=directory/(aid+'.png')
  if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Shared-image symlink blocked.')
  path.write_bytes(data);path.chmod(0o600);paths.append(str(path))
 return paths
