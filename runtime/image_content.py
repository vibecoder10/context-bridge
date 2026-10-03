"""Actual image pixels for bounded read tools, in each engine's native format."""
import base64
import json
from relay import BridgeError

def image_result(data, metadata, mime=None):
    if not data or len(data)>10*1024*1024:
        raise BridgeError('Images must be at most 10 MiB.')
    detected = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/gif' if data.startswith((b'GIF87a',b'GIF89a')) else
                'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else None)
    if not detected or (mime and mime!=detected):
        raise BridgeError('Choose a supported image with matching file contents.')
    return {'_image_content':[
        {'type':'text','text':json.dumps(metadata)},
        {'type':'image','mimeType':detected,'data':base64.b64encode(data).decode('ascii')},
    ]}

def mcp_content(value):
    return value['_image_content'] if '_image_content' in value else [{'type':'text','text':json.dumps(value)}]

def codex_content(value):
    return [({'type':'inputImage','imageUrl':'data:'+item['mimeType']+';base64,'+item['data']}
             if item['type']=='image' else {'type':'inputText','text':item['text']})
            for item in mcp_content(value)]
