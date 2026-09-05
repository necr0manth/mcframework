from mcframework.model import PackState


def test_pack_state_exposes_independent_consumer_projections():
    state = PackState()

    assert state.content.files == state.files
    assert state.content.exclusions == state.exclusions
    assert state.content.prune == state.prune

    assert state.package_catalog.packages == state.packages
    assert state.package_catalog.dependencies == state.dependencies
    assert state.package_catalog.groups == state.groups
    assert state.package_catalog.sections == state.sections

    assert state.web_policy.ignore == state.ignore


def test_projections_are_snapshots_and_do_not_mutate_aggregate():
    state = PackState()
    content = state.content
    catalog = state.package_catalog
    policy = state.web_policy

    assert content is not state
    assert catalog is not state
    assert policy is not state
