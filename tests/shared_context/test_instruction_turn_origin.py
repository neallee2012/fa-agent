"""Turn-origin metadata is trusted attribution, never an operation permission."""



from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


def test_origin_dtos_and_result_codecs_do_not_require_the_copilot_sdk():
    from cold_cli_helpers import cold_cli

    result = cold_cli(
        "import sys; sys.modules['copilot']=None; "
        "from shared_context.behavior_assistant_wire import InstructionContextOrigin,parse_instruction_context; "
        "from shared_context.behavior_turn_origin import InstructionTurnOrigin; "
        "print('ORIGIN_CODECS_IMPORTED')"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ORIGIN_CODECS_IMPORTED\n" and result.stderr == ""
