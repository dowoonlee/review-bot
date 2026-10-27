"""review-bot sandbox: there is no Docker daemon, by design.

Code that merely builds a docker-py client at import/initialization time (for example
a lazy app state) gets a client without contacting the daemon. Any real API call still
fails, as a DockerException carrying MARKER, so a test that needs Docker stays failed
and is reported as an environment limit rather than passing by accident.
"""
import importlib.abc
import importlib.util
import sys

MARKER = 'review-bot: the validation sandbox provides no Docker daemon'


def _patch(module):
    from docker import constants, errors
    import requests

    def _retrieve_server_version(self):
        return constants.DEFAULT_DOCKER_API_VERSION

    original_send = module.APIClient.send

    def send(self, request, **kwargs):
        try:
            return original_send(self, request, **kwargs)
        except requests.exceptions.ConnectionError as exc:
            raise errors.DockerException('%s (%s)' % (MARKER, exc)) from exc

    module.APIClient._retrieve_server_version = _retrieve_server_version
    module.APIClient.send = send


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != 'docker.api.client':
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        execute = spec.loader.exec_module

        def exec_module(module):
            execute(module)
            _patch(module)
        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _Finder())
