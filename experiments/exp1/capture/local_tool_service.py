"""Local real ATSuite RPC frontend; generated code only runs in Docker workers."""
import argparse,os
from http.server import ThreadingHTTPServer


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=9200);p.add_argument('--family',choices=['faas','mcp'],default='faas');a=p.parse_args()
    os.environ['ATSUITE_NODE_MODULE']='benchmarks.DataSciBench.nodes.AgentTools.implementation'
    os.environ['ATSUITE_RUNTIME']='mcp' if a.family=='mcp' else 'function'
    if a.family=='mcp':
        import importlib
        tools=importlib.import_module(os.environ['ATSUITE_NODE_MODULE'])
        from atsuite_sdk.mcp_server import server,MCPRequestLogMiddleware
        from starlette.middleware import Middleware
        import uvicorn
        app=server.http_app(path='/mcp',stateless_http=False,transport='http',middleware=[Middleware(MCPRequestLogMiddleware)])
        from contextlib import asynccontextmanager
        original_lifespan=app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(application):
            async with original_lifespan(application):
                try:yield
                finally:
                    if tools._mcp_kernel:tools._mcp_kernel.close()
        app.router.lifespan_context=lifespan
        uvicorn.run(app,host='127.0.0.1',port=a.port)
    else:
        from atsuite_sdk.function import create_handler
        ThreadingHTTPServer(('127.0.0.1',a.port),create_handler()).serve_forever()


if __name__=='__main__':main()
