"""
Copyright (c) 2016 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from __future__ import unicode_literals

from copy import deepcopy
import requests
import requests.auth
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

from atomic_reactor.plugin import ExitPlugin, PluginFailedException
from atomic_reactor.util import Dockercfg


class DeleteFromRegistryPlugin(ExitPlugin):
    """
    Delete previously pushed v2 images from a registry.
    """

    key = "delete_from_registry"
    is_allowed_to_fail = False

    def __init__(self, tasker, workflow, registries):
        """
        :param tasker: DockerTasker instance
        :param workflow: DockerBuildWorkflow instance
        :param registries: dict, keys are docker registries, values are dicts containing
                           per-registry parameters.
                           Params:
                            * "secret" optional string - path to the secret, which stores
                              login and password for remote registry
        """
        super(DeleteFromRegistryPlugin, self).__init__(tasker, workflow)

        self.registries = deepcopy(registries)


    def request_delete(self, url, manifest, insecure, auth):
        response = requests.delete(url, verify=not insecure, auth=auth)

        if response.status_code == requests.codes.ACCEPTED:
            self.log.info("deleted manifest %s", manifest)
            return True
        elif response.status_code == requests.codes.NOT_FOUND:
            self.log.warning("cannot delete %s: not found", manifest)
        elif response.status_code == requests.codes.METHOD_NOT_ALLOWED:
            self.log.warning("cannot delete %s: image deletion disabled on registry",
                             manifest)
        else:
            msg = "failed to delete %s: %s" % (manifest, response.reason)
            self.log.error("%s\n%s", msg, response.text)
            raise PluginFailedException(msg)

        return False


    def make_manifest(self, registry, repo, digest):
        manifest = registry + "/" + repo + "@" + digest
        return manifest

    def make_url(self, registry, repo, digest):
        url = registry + "/v2/" + repo + "/manifests/" + digest
        return url


    def get_worker_digests(self):
        """
         If we are being called from an orchestrator build, collect the worker
         node data and recreate the data locally.
        """
        try:
            builds = self.workflow.build_result.annotations['worker-builds']
        except(TypeError, KeyError):
            # This annotation is only set for the orchestrator build.
            # It's not present, so this is a worker build.
            return {}

        worker_digests = {}

        for plat, annotation in builds.items():
            digests = annotation['digests']
            self.log.debug("build %s has digests: %s" % (plat, digests))

            for digest in digests:
                reg = digest['registry']
                if reg in worker_digests:
                    worker_digests[reg].append(digest)
                else:
                    worker_digests[reg] = [digest]

        return worker_digests

    def run(self):
        deleted_digests = set()

        worker_digests = self.get_worker_digests()

        for registry, registry_conf in self.registries.items():
            if not registry.startswith('http://') and not registry.startswith('https://'):
                registry = 'https://' + registry

            registry_noschema = urlparse(registry).netloc

            auth = None
            insecure = registry_conf.get('insecure', False)
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

            if registry_noschema in worker_digests:
                digests = worker_digests[registry_noschema]
                for digest in digests:
                    if digest['digest'] in deleted_digests:
                        # Manifest schema version 2 uses the same digest
                        # for all tags
                        self.log.info('digest already deleted %s', digest['digest'])
                        continue

                    url = self.make_url(registry, digest['repository'], digest['digest'])
                    manifest = self.make_manifest(registry_noschema, digest['repository'], digest['digest'])

                    if self.request_delete(url, manifest, insecure, auth):
                        deleted_digests.add(digest['digest'])

                # If we are in orchestrator and found a match, good chance it
                # will not be in the workflow.push_conf dict.  Just continue.
                continue

            for push_conf_registry in self.workflow.push_conf.docker_registries:
                if push_conf_registry.uri == registry_noschema:
                    break
            else:
                self.log.warning("requested deleting image from %s but we haven't pushed there",
                                 registry_noschema)
                continue

            if push_conf_registry.insecure:
                #override insecure if passed
                insecure = push_conf_registry.insecure

            deleted = False
            for tag, digests in push_conf_registry.digests.items():
                digest = digests.default
                if digest in deleted_digests:
                    # Manifest schema version 2 uses the same digest
                    # for all tags
                    self.log.info('digest already deleted %s', digest)
                    continue

                repo = tag.split(':')[0]
                url = self.make_url(registry, repo, digest)
                manifest = self.make_manifest(registry_noschema, repo, digest)

                if self.request_delete(url, manifest, insecure, auth):
                    deleted_digests.add(digest)
                    deleted = True

            # delete these temp registries
            if deleted:
                self.workflow.push_conf.remove_docker_registry(push_conf_registry)

        return deleted_digests
