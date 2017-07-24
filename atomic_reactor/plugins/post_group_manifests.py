"""
Copyright (c) 2017 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.

get the image manifest lists from the worker builders. If possible, group them together
and return them. if not, return the x86_64/amd64 one.
"""


from __future__ import unicode_literals
from copy import deepcopy
import requests
import requests.auth

from six.moves.urllib.parse import urlparse

from atomic_reactor.plugin import PostBuildPlugin, PluginFailedException
from atomic_reactor.util import Dockercfg, get_manifest_digests, get_config_from_registry


class GroupManifestsPlugin(PostBuildPlugin):
    key = 'group_manifests'
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries, group=True, goarch=None):
        """
        constructor

        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "secret" optional string - path to the secret, which stores
                              login and password for remote registry
        :param group: bool, if true, return the grouped image manifests from the workers
        :param goarch: dict, keys are platform, values are go language platform names
        """
        # call parent constructor
        super(GroupManifestsPlugin, self).__init__(tasker, workflow)
        self.group = group
        self.goarch = goarch or {}
        self.registries = deepcopy(registries)
        self.worker_registries = {}

    def get_worker_manifest(self, worker_data):
        worker_digests = worker_data['digests']
        worker_manifest = []

        msg = "worker_registries {0}".format(self.worker_registries)
        self.log.debug(msg)

        for registry, registry_conf in self.registries.items():
            if registry_conf.get('version') == 'v1':
                continue

            if not registry.startswith('http://') and not registry.startswith('https://'):
                registry = 'https://' + registry

            registry_noschema = urlparse(registry).netloc
            self.log.debug("evaluating registry %s", registry_noschema)

            insecure = registry_conf.get('insecure', False)
            auth = None
            secret_path = registry_conf.get('secret')
            if secret_path:
                self.log.debug("registry %s secret %s", registry_noschema, secret_path)
                dockercfg = Dockercfg(secret_path).get_credentials(registry_noschema)
                try:
                    username = dockercfg['username']
                    password = dockercfg['password']
                except KeyError:
                    self.log.error("credentials for registry %s not found in %s",
                                   registry_noschema, secret_path)
                else:
                    self.log.debug("found user %s for registry %s", username, registry_noschema)
                    auth = requests.auth.HTTPBasicAuth(username, password)

            if registry_noschema in self.worker_registries:
                self.log.debug("getting manifests from %s", registry_noschema)
                digest = worker_digests[0]['digest']
                repo = worker_digests[0]['repository']

                url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, digest)
                self.log.debug("attempting get from %s", url)
                response = requests.get(url, verify=not insecure, auth=auth)

                image_manifest = response.json()

                if image_manifest['schemaVersion'] == '1':
                    msg = 'invalid schema from {0}'.format(url)
                    raise PluginFailedException(msg)

                push_conf_registry = self.workflow.push_conf.add_docker_registry(registry,
                                                                                 insecure=insecure)

                registry_image = None
                for image in self.workflow.tag_conf.images:
                    image_tag = image.to_str(registry=False)
                    url = '{0}/v2/{1}/manifests/{2}'.format(registry, repo, image_tag)
                    self.log.debug("for image_tag %s, putting at %s", image_tag, url)
                    response = requests.put(url, response, verify=not insecure, auth=auth)
                    registry_image = image.copy()
                    registry_image.registry = registry
                    self.log.debug("getting manifest_digests for %s", image_tag)
                    manifest_digests = get_manifest_digests(registry_image, registry,
                                                            insecure, secret_path)
                    push_conf_registry.digests[image_tag] = manifest_digests

                if registry_image:
                    push_conf_registry.config = get_config_from_registry(
                        registry_image, registry, manifest_digests, insecure, secret_path, 'v2')

                worker_manifest.append(image_manifest)
                self.log.debug("appending an image_manifest")
                break

        return worker_manifest

    def run(self):
        if self.group:
            raise NotImplementedError('group=True is not supported in group_manifest')
        grouped_manifests = []

        valid = False
        all_annotations = self.workflow.build_result.annotations['worker-builds']
        for plat, annotation in all_annotations.items():
            digests = annotation['digests']
            for digest in digests:
                registry = digest['registry']
                if registry in self.worker_registries:
                    self.worker_registries[registry].append(registry)
                else:
                    self.worker_registries[registry] = [registry]

        for platform in all_annotations:
            if self.goarch.get(platform, platform) == 'amd64':
                valid = True
                grouped_manifests = self.get_worker_manifest(all_annotations[platform])
                break

        if valid:
            self.log.debug("found an x86_64 platform and grouped its manifest")
            return grouped_manifests
        else:
            raise ValueError('failed to find an x86_64 platform')
