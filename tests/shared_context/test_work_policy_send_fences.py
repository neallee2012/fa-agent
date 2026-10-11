from datetime import timedelta
from types import SimpleNamespace
import httpx
import pytest
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.errors import Problem
from test_behavior_foundry import TARGET,INVOCATION,PERMIT,NOW


def test_native_start_checks_work_deadline_after_token_before_the_only_post():
    time=[NOW];deadline=NOW+timedelta(seconds=240);calls=[];checks=[]
    class Token:
        def get_token(self,_):
            time[0]=deadline
            return SimpleNamespace(token="OFFLINE-no-real-credential")
    def final():
        checks.append(True)
        if time[0]>=deadline:
            raise Problem(403,"WORK_EXECUTION_DEADLINE","original work permission expired")
    client=FoundryHttpClient(TARGET,Token(),transport=httpx.MockTransport(lambda r:calls.append(r)))
    try:
        with pytest.raises(Problem) as denied:
            client.start(INVOCATION,"original-launch","original-binding",PERMIT,before_send=final)
        assert denied.value.body["error"]["code"]=="WORK_EXECUTION_DEADLINE"
        assert checks==[True] and calls==[]
    finally:
        client.close()
