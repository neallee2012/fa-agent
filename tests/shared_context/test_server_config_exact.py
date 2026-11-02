"""``ServerConfiguration.exact``: every rule, by its own message, and the order the rules are checked in."""

from copy import deepcopy
import json

from pydantic import ValidationError
import pytest

from shared_context.behavior_server_config import ServerConfiguration, synthetic_configuration
from shared_context.source_materials import digest
from test_behavior_assistant_server import assistant_configuration
from test_behavior_server import projection_setup

SELECTION = "assistant settings require explicit assistant-v1.2 workload selection"
ASSISTANT_RESOURCES = "assistant requires C1, new readiness versions and exact existing API-UAMI/M3/provider resources"
ASSISTANT_PROFILES = "every assistant binding requires its own profile"
ASSISTANT_MAPPING = "assistant identity, exact checks, source or legacy-work mapping differs"
JOBS = "C1/C4 require distinct bounded jobs; C4 retains four slots and a fifth root job"
WORKLOAD = "every registered job needs its own supported workload definition"
DELIVERY = "worker/delivery/native model-repair configuration differs"
TARGETS = "four distinct exact Foundry agent/MI/session targets required"
OBSERVER = "Hosted agents cannot be their own readiness observer"
NAMESPACE = "source/backup authority, tenant or retained namespace differs"
CONTAINERS = "source and backup containers must be separate"
DUPLICATES = "duplicate/missing server bindings, roots, aliases or policies"
BINDING = "fixed identity/root/deterministic work contract differs"
ALIASES = "operation aliases must resolve only their own fixed work"
UNUSED = "unused root or operation binding"
OWNERS = "owner/policy identities must be unambiguous"
POLICY = "runtime policy is not bound to this native binding"
PROJECTION_UNIQUE = "memory projection binding and namespace must be unique"
PROJECTION_BINDING = "memory projection references an unknown runtime binding"
PROJECTION = "memory projection must use the exact project/reviewed source/binding/context authority"
PINNED = "versions must be pinned"
ARM = "exact deployed API/controller ARM resources required"

OTHER_OID = "77777777-7777-7777-7777-777777777777"
CHILD = {"job_id": "child-job", "work": {"work_id": "child-work", "goal": "Run the child.",
                                         "criteria_version": "child-criteria-v1", "result_kind": "answer", "human_checks": [],
                                         "required_checks": []}}


def rejected(raw):
    with pytest.raises(ValidationError) as caught:
        ServerConfiguration.model_validate_json(json.dumps(raw))
    errors = caught.value.errors()
    assert len(errors) == 1 and errors[0]["loc"] == ()  # the top-level rule, not a nested model's own
    return errors[0]["msg"].removeprefix("Value error, ")


def synthetic():
    return synthetic_configuration().model_dump(mode="json")


def with_projection():
    config = synthetic_configuration()
    raw = config.model_dump(mode="json")
    raw["memory_projections"] = [projection_setup(config).model_dump(mode="json")]
    return raw


def child_of(raw, **update):
    work = raw["bindings"][0]["work"]
    child = deepcopy(CHILD)
    child["work"]["required_checks"] = list(work["required_checks"])
    child["job_id"] = update.pop("job_id", child["job_id"])
    child["work"].update(update)
    raw["bindings"][0]["child_jobs"] = [child]


def drop_bindings(raw, keep):
    for field in ("bindings", "policies", "operations"):
        raw[field] = raw[field][:keep]


def many_jobs(raw):
    raw["workers"].update(pending_limit=32)
    work = raw["bindings"][0]["work"]
    for binding in raw["bindings"]:
        binding["child_jobs"] = [
            {"job_id": f"{binding['binding_id']}-child-{index}",
             "work": {**work, "work_id": f"{binding['binding_id']}-work-{index}"}}
            for index in range(16)]


def shared_project(raw):
    raw["targets"][1].update(agent_name=raw["targets"][0]["agent_name"])


def other_host(raw):
    raw["targets"][2].update(project_endpoint="https://elsewhere.example/api/projects/project")


def other_group(account):
    return account.replace("/resourceGroups/synthetic/", "/resourceGroups/other/")


def duplicate_binding_id(raw):
    raw["bindings"][1].update(binding_id=raw["bindings"][0]["binding_id"])
    raw["policies"].pop(1)
    raw["operations"][1].update(binding_id=raw["bindings"][0]["binding_id"])


def duplicate_root(raw):
    raw["roots"].append(dict(raw["roots"][0]))


def extra_root(raw):
    raw["roots"].append({**raw["roots"][0], "root_budget_id": "unused"})


def duplicate_policy(raw):
    raw["policies"].append(deepcopy(raw["policies"][0]))


def duplicate_alias(raw):
    raw["operations"].append(deepcopy(raw["operations"][0]))


def stranger_agent(raw):
    raw["bindings"][0].update(agent_oid=OTHER_OID)
    raw["bindings"][0]["owner"].update(principal_id=OTHER_OID)


def other_human(raw):
    raw["bindings"][0].update(human_oid=OTHER_OID)
    raw["bindings"][0]["route"].update(recipient_id=OTHER_OID)


def other_tenant(raw):
    tenant = "22222222-2222-2222-2222-222222222222"
    raw["bindings"][0].update(tenant_id=tenant)
    raw["bindings"][0]["route"].update(tenant_id=tenant)


def unknown_operation_binding(raw):
    raw["operations"].append({**raw["operations"][0], "input_ref": "stray", "binding_id": "unknown"})


def second_projection(raw, **update):
    raw["memory_projections"].append({**deepcopy(raw["memory_projections"][0]), **update})


def private_projection(raw, owner):
    raw["policies"][0]["policy"].update(source_visibility="private")
    raw["memory_projections"][0]["scope"].update(visibility="private", owner_actor=owner)


def rebound(edit):
    """``edit``, then each policy re-signed to its changed binding, so the rule after the digest check is reached."""
    def apply(raw):
        edit(raw)
        for policy, binding in zip(raw["policies"], raw["bindings"]):
            policy["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", binding])
    return apply


CASES = [
    (synthetic, lambda c: c.update(workload_version="assistant-v1.2"), SELECTION),
    (assistant_configuration, lambda c: c.update(workload_version="checksum-count-v1"), SELECTION),

    (assistant_configuration, lambda c: c["workers"].update(capacity=4), ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: child_of(c), ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["workers"].update(executor_version="checksum-count-v1"), ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["workers"].update(protection_version="retention-v1"), ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["delivery"].update(protection_version="retention-v1"), ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["identity"].update(managed_identity_client_id=None), ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["assistant"].update(provider_endpoint="https://other.example/v1"),
     ASSISTANT_RESOURCES),
    (assistant_configuration,
     lambda c: c["assistant"]["m3_context"].update(account_resource_id=other_group(c["table"]["account_resource_id"])),
     ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["assistant"]["m3_context"].update(endpoint=c["table"]["endpoint"] + "#"),
     ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["assistant"]["m3_context"].update(table_name="OtherTable"),
     ASSISTANT_RESOURCES),
    (assistant_configuration, lambda c: c["assistant"].update(artifact_container_url="https://other.example/c"),
     ASSISTANT_RESOURCES),

    (assistant_configuration, lambda c: c["assistant"]["profiles"][0].update(binding_id="other"), ASSISTANT_PROFILES),

    (assistant_configuration, lambda c: c["targets"].reverse(), ASSISTANT_MAPPING),
    (assistant_configuration, lambda c: c["assistant"]["profiles"][0].update(claim_id="unreviewed"), ASSISTANT_MAPPING),
    (assistant_configuration, lambda c: c["assistant"]["profiles"][0]["source"].update(source_id="unassigned"),
     ASSISTANT_MAPPING),
    (assistant_configuration,
     lambda c: c["assistant"]["profiles"][0].update(legacy_source_work=c["bindings"][0]["work"]["work_id"]),
     ASSISTANT_MAPPING),
    (assistant_configuration, lambda c: c["bindings"][0]["work"]["required_checks"].reverse(), ASSISTANT_MAPPING),

    (synthetic, lambda c: drop_bindings(c, 4), JOBS),
    (synthetic, lambda c: c["workers"].update(pending_limit=4), JOBS),
    (synthetic, many_jobs, JOBS),
    (synthetic, lambda c: (child_of(c), c["workers"].update(max_generations=1)), JOBS),
    (synthetic, lambda c: c["bindings"][1].update(job_id=c["bindings"][0]["job_id"]), JOBS),
    (synthetic, lambda c: c["bindings"][1]["work"].update(work_id=c["bindings"][0]["work"]["work_id"]), JOBS),

    (synthetic, lambda c: c["bindings"][0]["work"].update(required_checks=["answer_verified"]), WORKLOAD),
    (synthetic, lambda c: c["bindings"][0]["work"].update(
        human_checks=c["bindings"][0]["work"]["required_checks"][:1]), WORKLOAD),
    (synthetic, lambda c: c["bindings"][0]["work"].update(result_kind="artifact"), WORKLOAD),
    (synthetic, lambda c: child_of(c, result_kind="artifact"), WORKLOAD),

    (synthetic, lambda c: c["delivery"].update(team_id="other-team"), DELIVERY),
    (synthetic, lambda c: c["delivery"].update(control_binding="f" * 64), DELIVERY),
    (synthetic, lambda c: c["delivery"].update(protection_version="other-v1"), DELIVERY),
    (synthetic, lambda c: c["delivery"].update(model_repair_contract="offline-model-budget-v1"), DELIVERY),

    (synthetic, lambda c: c["targets"][0].update(agent_oid=OTHER_OID), TARGETS),
    (synthetic, lambda c: c["targets"][1].update(agent_oid=c["targets"][0]["agent_oid"]), TARGETS),
    (synthetic, lambda c: c["targets"][1].update(agent_id=c["targets"][0]["agent_id"]), TARGETS),
    (synthetic, lambda c: c["targets"][1].update(executor_id=c["targets"][0]["executor_id"]), TARGETS),
    (synthetic, lambda c: c["targets"][1].update(session_id=c["targets"][0]["session_id"]), TARGETS),
    (synthetic, shared_project, TARGETS),
    (synthetic, other_host, TARGETS),

    (synthetic, lambda c: c["evidence"].update(observer_oid=c["identity"]["agents"][2]["oid"]), OBSERVER),

    (synthetic, lambda c: c["sources"]["protection"].update(authority_domain="other"), NAMESPACE),
    (synthetic, lambda c: c["backups"]["protection"].update(authority_domain="other"), NAMESPACE),
    (synthetic, lambda c: c["sources"]["protection"].update(
        protected_prefix=c["backups"]["protection"]["protected_prefix"]), NAMESPACE),
    (synthetic, lambda c: c["backups"]["protection"].update(
        protected_prefix=c["sources"]["protection"]["protected_prefix"]), NAMESPACE),

    (synthetic, lambda c: c["backups"]["protection"].update(container_url=c["sources"]["protection"]["container_url"]),
     CONTAINERS),

    (synthetic, duplicate_binding_id, DUPLICATES),
    (synthetic, duplicate_root, DUPLICATES),
    (synthetic, duplicate_policy, DUPLICATES),
    (synthetic, lambda c: c["policies"].pop(), DUPLICATES),
    (synthetic, lambda c: c["policies"][0].update(binding_id="unknown"), DUPLICATES),
    (synthetic, duplicate_alias, DUPLICATES),

    (synthetic, other_tenant, BINDING),
    (synthetic, lambda c: c["bindings"][0].update(team_id="other-team"), BINDING),
    (synthetic, other_human, BINDING),
    (synthetic, stranger_agent, BINDING),
    (synthetic, lambda c: c["bindings"][0]["owner"].update(root_budget_id="unknown"), BINDING),
    (synthetic, lambda c: c["bindings"][0].update(root_request_id="other-root"), BINDING),

    (synthetic, lambda c: c["operations"][0].update(input_ref="another-alias"), ALIASES),
    (synthetic, lambda c: c["operations"][0].update(binding_id="binding-2"), ALIASES),

    (synthetic, extra_root, UNUSED),
    (synthetic, unknown_operation_binding, UNUSED),

    (synthetic, lambda c: c["bindings"][1]["owner"].update(owner_record_id="owner-1"), OWNERS),
    (synthetic, lambda c: c["bindings"][1]["owner"].update(policy_id="policy-1"), OWNERS),

    (synthetic, lambda c: c["policies"][0]["policy"].update(binding_digest="f" * 64), POLICY),
    (synthetic, lambda c: c["policies"][4]["policy"].update(binding_digest="f" * 64), POLICY),

    (with_projection, lambda c: second_projection(c, projection_namespace="fa-reviewed-v1-other"), PROJECTION_UNIQUE),
    (with_projection, lambda c: second_projection(c, binding_id="binding-2"), PROJECTION_UNIQUE),

    (with_projection, lambda c: c["memory_projections"][0].update(binding_id="unknown"), PROJECTION_BINDING),

    (with_projection, lambda c: c["memory_projections"][0].update(control_binding="f" * 64), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0].update(
        endpoint="https://other.services.ai.azure.com/api/projects/other"), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0]["scope"].update(
        tenant_id="22222222-2222-2222-2222-222222222222"), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0]["scope"].update(team_id="other-team"), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0]["scope"].update(visibility="private", owner_actor="agent-1"),
     PROJECTION),
    (with_projection, lambda c: private_projection(c, "agent-2"), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0].update(subject="other"), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0].update(claim_ids=["unreviewed"]), PROJECTION),
    (with_projection, lambda c: c["memory_projections"][0]["versions"].update(catalog_version="other-v1"), PROJECTION),

    (synthetic, lambda c: c.update(review_policy_version="latest"), PINNED),
    (synthetic, lambda c: c.update(review_policy_version="LATEST"), PINNED),
    (synthetic, lambda c: c.update(review_policy_version="current"), PINNED),
    (synthetic, lambda c: c.update(review_policy_version="head"), PINNED),
    (synthetic, lambda c: c.update(review_policy_version="unknown"), PINNED),
    (synthetic, lambda c: c["delivery"].update(consumer_version="latest"), PINNED),
    (synthetic, lambda c: c["delivery"].update(timer_version="latest"), PINNED),
    (synthetic, lambda c: c["delivery"].update(verifier_version="latest"), PINNED),
    (synthetic, lambda c: c["sources"]["protection"].update(configuration_version="latest"), PINNED),
    (synthetic, lambda c: c["backups"]["protection"].update(configuration_version="latest"), PINNED),
    (synthetic, lambda c: c["sources"]["protection"].update(generation="latest"), PINNED),
    (synthetic, lambda c: c["backups"]["protection"].update(generation="latest"), PINNED),
    (synthetic, rebound(lambda c: c["bindings"][4]["work"].update(criteria_version="latest")), PINNED),
    (synthetic, rebound(lambda c: c["bindings"][4]["context_versions"].update(release_version="latest")), PINNED),
    (synthetic, rebound(lambda c: c["bindings"][4]["context_versions"].update(catalog_version="latest")), PINNED),
    (synthetic, rebound(lambda c: c["bindings"][4]["context_versions"].update(memory_schema_version="latest")), PINNED),
    (synthetic, rebound(lambda c: child_of(c, criteria_version="latest")), PINNED),

    (synthetic, lambda c: c.update(api_resource_id="/resourceGroups/synthetic/api"), ARM),
    (synthetic, lambda c: c.update(controller_resource_id="/resourceGroups/synthetic/controller"), ARM),
    (synthetic, lambda c: c.update(api_resource_id=c["api_resource_id"] + "*"), ARM),
    (synthetic, lambda c: c.update(controller_resource_id=c["controller_resource_id"] + "*"), ARM),
    (synthetic, lambda c: c.update(api_resource_id=c["api_resource_id"] + "?"), ARM),
    (synthetic, lambda c: c.update(controller_resource_id=c["controller_resource_id"] + "?"), ARM),
]


@pytest.mark.parametrize(("base", "mutation", "message"), CASES)
def test_each_rule_refuses_with_its_own_message(base, mutation, message):
    raw = base()
    mutation(raw)
    assert rejected(raw) == message


def test_unmutated_bases_pass():
    for base in (synthetic, assistant_configuration, with_projection):
        raw = base()
        assert ServerConfiguration.model_validate_json(json.dumps(raw)).model_dump(mode="json") == raw


@pytest.mark.parametrize("owner", ["agent-1", "human-99999999-9999-9999-9999-999999999999"])
def test_private_projection_may_be_owned_by_its_agent_or_human(owner):
    raw = with_projection()
    private_projection(raw, owner)
    ServerConfiguration.model_validate_json(json.dumps(raw))


def test_one_generation_is_enough_without_child_jobs():
    raw = synthetic()
    raw["workers"].update(max_generations=1)
    ServerConfiguration.model_validate_json(json.dumps(raw))


def test_a_projection_may_use_a_subset_of_its_bindings_claims():
    raw = with_projection()
    raw["bindings"][0]["claim_ids"].append("claim-extra")
    rebound(lambda _: None)(raw)
    ServerConfiguration.model_validate_json(json.dumps(raw))


def test_bounded_job_count_allows_exactly_the_pending_limit():
    raw = synthetic()
    raw["workers"].update(pending_limit=5)
    ServerConfiguration.model_validate_json(json.dumps(raw))


# Every rule's violation applied at once, latest first: repairing the earliest exposes the next, so the refusal
# a config sees cannot change with the order the rules run in.
CHAIN = [
    (lambda c: c["delivery"].update(team_id="other-team"), DELIVERY),
    (lambda c: c["targets"][1].update(session_id=c["targets"][0]["session_id"]), TARGETS),
    (lambda c: c["evidence"].update(observer_oid=c["identity"]["agents"][2]["oid"]), OBSERVER),
    (lambda c: c["sources"]["protection"].update(authority_domain="other"), NAMESPACE),
    (lambda c: c["backups"]["protection"].update(container_url=c["sources"]["protection"]["container_url"]), CONTAINERS),
    (duplicate_policy, DUPLICATES),
    (lambda c: c["bindings"][0].update(team_id="other-team"), BINDING),
    (lambda c: c["bindings"][1].update(allowed_input_refs=["other-alias"]), ALIASES),
    (extra_root, UNUSED),
    (lambda c: c["bindings"][1]["owner"].update(policy_id="policy-1"), OWNERS),
    (lambda c: c["policies"][4]["policy"].update(binding_digest="f" * 64), POLICY),
    (lambda c: second_projection(c, binding_id="binding-2"), PROJECTION_UNIQUE),
    (lambda c: c["memory_projections"][0].update(binding_id="unknown"), PROJECTION_BINDING),
    (lambda c: c["memory_projections"][0].update(subject="other"), PROJECTION),
    (lambda c: c.update(review_policy_version="latest"), PINNED),
    (lambda c: c.update(api_resource_id="/resourceGroups/synthetic/api"), ARM),
]


def test_rules_refuse_in_their_fixed_order():
    for start in range(len(CHAIN)):
        raw = with_projection()
        for mutation, _ in reversed(CHAIN[start:]):
            mutation(raw)
        assert rejected(raw) == CHAIN[start][1]


@pytest.mark.parametrize(("first", "second", "message"), [
    (lambda c: c.update(workload_version="checksum-count-v1"), lambda c: c["workers"].update(capacity=4), SELECTION),
    (lambda c: c["workers"].update(capacity=4), lambda c: c["assistant"]["profiles"][0].update(binding_id="other"),
     ASSISTANT_RESOURCES),
    (lambda c: c["assistant"]["profiles"][0].update(binding_id="other"),
     lambda c: c["bindings"][0]["work"].update(result_kind="artifact"), ASSISTANT_PROFILES),
    (lambda c: c["assistant"]["profiles"][0].update(claim_id="unreviewed"),
     lambda c: c["bindings"][0]["work"].update(result_kind="artifact"), ASSISTANT_MAPPING),
])
def test_assistant_rules_come_before_the_shared_rules(first, second, message):
    raw = assistant_configuration()
    first(raw)
    second(raw)
    assert rejected(raw) == message


def test_jobs_are_bounded_before_each_job_is_checked():
    raw = synthetic()
    drop_bindings(raw, 4)
    raw["bindings"][0]["work"].update(result_kind="artifact")
    assert rejected(raw) == JOBS


def test_each_job_is_checked_before_delivery():
    raw = synthetic()
    raw["bindings"][0]["work"].update(result_kind="artifact")
    raw["delivery"].update(team_id="other-team")
    assert rejected(raw) == WORKLOAD
