"""First-party MCP tool bound to one leased shared chat job. No shell/file tools."""
import json
import sys
import time
from pathlib import Path
from named_claude_listener import hooks
from shared_chat_images import shared_image
from image_content import image_result,mcp_content
from project_operator import ProjectOperator,installed_root

TOOL={'name':'delegate_image','description':'Queue an image with the requesting human’s Codex agent now, then continue your own script/work. Does not wait for image completion. Use only if the human requested an image. Target must be an exact registered image-capable handle. Keep the same key for retries.', 'inputSchema':{'type':'object','properties':{'target':{'type':'string'},'text':{'type':'string'},'idempotency_key':{'type':'string'}},'required':['target','text','idempotency_key'],'additionalProperties':False}}
VIEW={'name':'view_chat_image','description':'View the actual pixels of a published image attached to this shared job before giving visual feedback. This reads no private folder or arbitrary URL.','inputSchema':{'type':'object','properties':{'artifact_id':{'type':'string'}},'required':['artifact_id'],'additionalProperties':False}}
def serve(connection,context):
 agent=hooks.AgentHooks(connection)
 work=json.loads(Path(context).read_text());job=work['job']
 operator=ProjectOperator(agent) if installed_root(agent) and job.get('owner_request') is True else None
 if operator:operator.bind(work)
 for line in sys.stdin:
  request=json.loads(line)
  if 'id' not in request:continue
  method=request.get('method');params=request.get('params',{})
  if method=='initialize':result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'context-bridge-delegate','version':'1.0'}}
  elif method=='tools/list':result={'tools':[TOOL,VIEW]+(operator.tools if operator else [])}
  elif method=='ping':result={}
  elif method=='tools/call':
   try:
    args=params.get('arguments',{})
    if operator and params.get('name') in [t['name'] for t in operator.tools]:
     result={'content':mcp_content(operator.run(params['name'],args)),'isError':False}
    elif params.get('name')=='view_chat_image' and set(args)=={'artifact_id'}:
     data,meta=shared_image(agent,work,args['artifact_id'])
     hooks.private_json(Path(context).with_name('image-tool-audit.json'),{'job_id':job['id'],'artifact_id':meta['id'],'sha256':meta['sha256'],'tool':'view_chat_image','checked_at':time.time()})
     result={'content':mcp_content(image_result(data,{'artifact_id':meta['id'],'sha256':meta['sha256']})),'isError':False}
    elif params.get('name')=='delegate_image' and set(args)=={'target','text','idempotency_key'}:
     value=agent.request('delegate_image',job_id=job['id'],lease=job['lease'],**args)
     result={'content':[{'type':'text','text':json.dumps(value)}],'isError':False}
    else:raise ValueError('Only shared-image viewing and delegation are available.')
   except Exception as error:
    message=str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Tool failed; inspect the actual grant or prerequisite before claiming completion.'
    result={'content':[{'type':'text','text':message}],'isError':True}
  else:
   print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
if __name__=='__main__':serve(sys.argv[1],sys.argv[2])
