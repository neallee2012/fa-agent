"""In-process httpx transport over a FastAPI TestClient (moved from shared_context.behavior_assistant)."""


class _LocalHttp:
    def __init__(self, client):
        self.client = client

    def handle_request(self, request):
        import httpx
        response = self.client.request(request.method, request.url.raw_path.decode(),
                                       content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)

    def close(self):
        pass
