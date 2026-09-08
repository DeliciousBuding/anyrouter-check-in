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

		async def rotate(self, excluded):
			assert 'node-a' in excluded
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
