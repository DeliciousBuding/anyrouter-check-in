import pytest

from utils.egress import EgressController, EgressRotator, node_label


@pytest.mark.asyncio
async def test_list_nodes_filters_control_entries():
	controller = EgressController('http://127.0.0.1:9097', 'secret')

	async def fake_request(method, path, *, payload=None):
		return {'all': ['CHECKIN_AUTO', 'node-a', 'DIRECT', 'node-b', 'REJECT']}

	setattr(controller, '_request', fake_request)
	assert await controller.list_nodes() == ['node-a', 'node-b']


@pytest.mark.asyncio
async def test_rotate_skips_current_and_excluded_nodes():
	controller = EgressController('http://127.0.0.1:9097', 'secret')
	selected = []

	async def fake_request(method, path, *, payload=None):
		if method == 'GET':
			return {'all': ['node-a', 'node-b', 'node-c'], 'now': 'node-a'}
		selected.append(payload['name'])
		return {}

	setattr(controller, '_request', fake_request)
	assert await controller.rotate({'node-b'}) == 'node-c'
	assert selected == ['node-c']


@pytest.mark.asyncio
async def test_rotator_excludes_current_node_and_enforces_budget():
	class FakeController(EgressController):
		auto_group = 'CHECKIN_AUTO'

		def __init__(self):
			super().__init__('http://127.0.0.1:1', 'secret')
			self.current = 'node-a'
			self.selected = []

		async def current_node(self):
			return self.current

		async def select_stable_node(self, account_key, excluded=None):
			assert 'node-a' in (excluded or set())
			self.selected.append('node-b')
			return 'node-b'

	controller = FakeController()
	rotator = EgressRotator(controller, max_rotations=1)
	node, label = await rotator.rotate()
	assert node == 'node-b'
	assert label == node_label('node-b')
	assert controller.selected == ['node-b']
	assert await rotator.rotate() == (None, None)


def test_node_label_does_not_expose_node_name():
	label = node_label('private-node-name')
	assert len(label) == 8
	assert 'private-node-name' not in label


def test_controller_from_env(monkeypatch):
	monkeypatch.setenv('CHECKIN_PROXY_API_URL', 'http://127.0.0.1:9097/')
	monkeypatch.setenv('CHECKIN_PROXY_API_SECRET', 'local-secret')
	controller = EgressController.from_env()
	assert controller is not None
	assert controller.api_url == 'http://127.0.0.1:9097'
	assert controller.group == 'CHECKIN'
	assert controller.auto_group == 'CHECKIN_AUTO'


@pytest.mark.asyncio
async def test_stable_node_selection_is_per_account():
	controller = EgressController('http://127.0.0.1:9097', 'secret')
	selected = []

	async def fake_list_nodes():
		return ['node-a', 'node-b', 'node-c']

	async def fake_current_node():
		return None

	async def fake_select_node(name):
		selected.append(name)

	setattr(controller, 'list_nodes', fake_list_nodes)
	setattr(controller, 'current_node', fake_current_node)
	setattr(controller, 'select_node', fake_select_node)

	first = await controller.select_stable_node('agentrouter:account-a')
	second = await controller.select_stable_node('agentrouter:account-a')

	assert first == second
	assert len(selected) == 2


@pytest.mark.asyncio
async def test_stable_node_selection_reuses_current_node():
	controller = EgressController('http://127.0.0.1:9097', 'secret')

	async def fake_list_nodes():
		return ['node-a', 'node-b']

	async def fake_current_node():
		return 'node-b'

	async def fail_select_node(name):
		raise AssertionError(f'selected {name} despite a usable current node')

	setattr(controller, 'list_nodes', fake_list_nodes)
	setattr(controller, 'current_node', fake_current_node)
	setattr(controller, 'select_node', fail_select_node)

	assert await controller.select_stable_node('agentrouter:account-a') == 'node-b'
