# SPDX-FileCopyrightText: 2024 SAP SE or an SAP affiliate company and Gardener contributors
#
# SPDX-License-Identifier: Apache-2.0


import logging
import threading
import typing

import requests

import ccc.concourse
import ccc.github
import ccc.secrets_server
import ci.util
import concourse.client.api
import concourse.client.model
import concourse.enumerator
import concourse.replicator
import model
import whd.model
import whd.pull_request
import whd.util

from github3.exceptions import NotFoundError

from .pipelines import replicate_repository_pipelines
from concourse.client.util import determine_jobs_to_be_triggered
from concourse.enumerator import JobMappingNotFoundError
from concourse.model.job import AbortObsoleteJobs
from model import ConfigFactory
from model.base import ConfigElementNotFoundError
from model.webhook_dispatcher import WebhookDispatcherConfig

from concourse.client.model import (
    ResourceType,
)
from .model import (
    AbortConfig,
    Pipeline,
    PullRequestAction,
    PullRequestEvent,
    PushEvent,
    RefType,
)


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class GithubWebhookDispatcher:
    def __init__(
        self,
        cfg_factory,
        cfg_set,
        whd_cfg: WebhookDispatcherConfig
    ):
        self.cfg_factory: model.ConfigFactory = cfg_factory
        self.cfg_set = cfg_set
        self.whd_cfg = whd_cfg
        logger.info(f'github-whd initialised for cfg-set: {cfg_set.name()}')

    def concourse_clients(
        self,
    ) -> typing.Generator[concourse.client.api.ConcourseApiBase, None, None]:
        for concourse_config_name in self.whd_cfg.concourse_config_names():
            concourse_cfg = self.cfg_factory.concourse(concourse_config_name)
            job_mapping_set = self.cfg_factory.job_mapping(concourse_cfg.job_mapping_cfg_name())
            for job_mapping in job_mapping_set.job_mappings().values():
                yield ccc.concourse.client_from_cfg_name(
                    concourse_cfg_name=concourse_cfg.name(),
                    team_name=job_mapping.team_name(),
                )

    def dispatch_create_event(
        self,
        create_event,
    ):
        ref_type = create_event.ref_type()
        if not ref_type == RefType.BRANCH:
            logger.info(f'ignored create event with type {ref_type}')
            return

        thread = threading.Thread(
            target=self._update_pipeline_definition,
            kwargs={
                'event': create_event,
            }
        )
        thread.start()

    def dispatch_push_event(
        self,
        push_event,
    ):
        def process_push_event(event):
            if self._pipeline_definition_changed(event):
                try:
                    self._update_pipeline_definition(event=event)
                except ValueError as e:
                    logger.warning(
                        f'Received error updating pipeline-definitions: "{e}". '
                        'Will still abort running jobs (if configured) and trigger resource checks.'
                    )

            self.abort_running_jobs_if_configured(event)

            for concourse_api in self.concourse_clients():
                logger.debug(f'using concourse-api: {concourse_api}')
                resources = self._matching_resources(
                    concourse_api=concourse_api,
                    event=event,
                )
                logger.debug('triggering resource-check')
                whd.util.trigger_resource_check(concourse_api=concourse_api, resources=resources)

        thread = threading.Thread(
            target=process_push_event,
            kwargs={
                'event': push_event,
            }
        )
        thread.start()

    def _update_pipeline_definition(
        self,
        event,
    ):
        def _do_update():
            repo_url = event.repository().repository_url()
            job_mapping_set = self.cfg_set.job_mapping()
            job_mapping = job_mapping_set.job_mapping_for_repo_url(repo_url, self.cfg_set)

            replicate_repository_pipelines(
                repo_url=repo_url,
                cfg_set=self.cfg_factory.cfg_set(job_mapping.replication_ctx_cfg_set()),
                whd_cfg=self.whd_cfg,
            )

        try:
            _do_update()
        except (JobMappingNotFoundError, ConfigElementNotFoundError) as e:
            # A config element was missing or o JobMapping for the given repository was present.
            # Print warning, reload and try again
            logger.warning(
                f'failed to update pipeline definition: {e}. Will reload config and try again.'
            )
            # Attempt to fetch latest cfg from SS and replace it
            raw_dict = ccc.secrets_server.SecretsServerClient.default().retrieve_secrets()
            self.cfg_factory = ConfigFactory.from_dict(raw_dict)
            self.cfg_set = self.cfg_factory.cfg_set(self.cfg_set.name())
            # retry
            _do_update()

    def _pipeline_definition_changed(self, push_event):
        if '.ci/pipeline_definitions' in push_event.modified_paths():
            return True
        return False

    def determine_affected_pipelines(self, push_event) -> typing.Generator[Pipeline, None, None]:
        '''yield each concourse pipeline that may be affected by the given push-event.
        '''
        repo = push_event.repository()
        repo_url = repo.repository_url()
        job_mapping_set = self.cfg_set.job_mapping()

        try:
            job_mapping = job_mapping_set.job_mapping_for_repo_url(repo_url, self.cfg_set)
        except ValueError:
            logger.info(f'no job-mapping found for {repo_url=} - will not interact w/ pipeline(s)')
            return

        try:
            repo_enumerator = concourse.enumerator.GithubRepositoryDefinitionEnumerator(
                repository_url=repo_url,
                cfg_set=self.cfg_factory.cfg_set(job_mapping.replication_ctx_cfg_set()),
            )
        except concourse.enumerator.JobMappingNotFoundError:
            logger.info(f'no job-mapping matched for {repo_url=} - will not interact w/ pipeline(s)')
            return

        try:
            definition_descriptors = [d for d in repo_enumerator.enumerate_definition_descriptors()]
        except NotFoundError:
            logger.warning(
                f"Unable to access repository '{repo_url}' on github '{repo.github_host()}'. "
                "Please make sure the repository exists and the technical user has the necessary "
                "permissions to access it."
            )
            definition_descriptors = []

        for descriptor in definition_descriptors:
            # need to merge and consider the effective definition
            effective_definition = descriptor.pipeline_definition
            for override in descriptor.override_definitions:
                effective_definition = ci.util.merge_dicts(effective_definition, override)

            yield Pipeline(
                pipeline_name=descriptor.effective_pipeline_name(),
                target_team=descriptor.concourse_target_team,
                effective_definition=effective_definition,
            )

    def matching_client(self, team):
        for c in self.concourse_clients():
            if c.routes.team == team:
                return c

    def abort_running_jobs_if_configured(self, push_event):
        builds_to_consider = 5
        for pipeline in self.determine_affected_pipelines(
            push_event
        ):
            if not (client := self.matching_client(pipeline.target_team)):
                logger.info(
                    f'no matching job-mapping for {pipeline.pipeline_name=} - skipping abortion'
                )
                continue

            try:
                pipeline_config = client.pipeline_cfg(pipeline.pipeline_name)
            except requests.exceptions.HTTPError as e:
                # might not exist yet if the pipeline was just rendered by the WHD
                if e.response.status_code != 404:
                    raise e
                logger.warning(f"could not retrieve pipeline config for '{pipeline.pipeline_name}'")
                continue

            resources = [
                r for r in pipeline_config.resources
                if ResourceType(r.type) in (ResourceType.GIT, ResourceType.PULL_REQUEST)
            ]
            for job in determine_jobs_to_be_triggered(*resources):
                if (
                    not pipeline.effective_definition['jobs'].get(job.name)
                    or not 'abort_outdated_jobs' in pipeline.effective_definition['jobs'][job.name]
                ):
                    continue
                abort_cfg = AbortConfig.from_dict(
                    pipeline.effective_definition['jobs'][job.name]
                )

                if abort_cfg.abort_obsolete_jobs is AbortObsoleteJobs.NEVER:
                    continue
                elif (
                    abort_cfg.abort_obsolete_jobs is AbortObsoleteJobs.ON_FORCE_PUSH_ONLY
                    and not push_event.is_forced_push()
                ):
                    continue
                elif abort_cfg.abort_obsolete_jobs is AbortObsoleteJobs.ALWAYS:
                    pass
                else:
                    raise NotImplementedError(abort_cfg.abort_obsolete_jobs)

                running_builds = [
                    b for b in client.job_builds(pipeline.pipeline_name, job.name)
                    if b.status() is concourse.client.model.BuildStatus.RUNNING
                ][:builds_to_consider]

                for build in running_builds:
                    if build.plan().contains_version_ref(push_event.previous_ref()):
                        logger.info(
                            f"Aborting obsolete build '{build.build_number()}' for job '{job.name}'"
                        )
                        client.abort_build(build.id())

    def dispatch_pullrequest_event(
        self,
        pr_event: whd.model.PullRequestEvent,
    ) -> bool:
        '''Process the given push event.

        Return `True` if event will be processed, `False` if no processing will be done.
        '''
        if not pr_event.action() in (
            PullRequestAction.OPENED,
            PullRequestAction.REOPENED,
            PullRequestAction.LABELED,
            PullRequestAction.SYNCHRONIZE,
        ):
            logger.info(f'ignoring pull-request action {pr_event.action()}')
            return False

        thread = threading.Thread(
            target=whd.pull_request.process_pr_event,
            kwargs={
                'concourse_clients': self.concourse_clients(),
                'cfg_factory': self.cfg_factory,
                'whd_cfg': self.whd_cfg,
                'cfg_set': self.cfg_set,
                'pr_event': pr_event,
            }
        )
        thread.start()

        return True

    def _matching_resources(
        self,
        concourse_api: concourse.client.api.ConcourseApiBase,
        event,
    ) -> typing.Generator[concourse.client.model.PipelineConfigResource, None, None]:
        if isinstance(event, PushEvent):
            resource_type = ResourceType.GIT
        elif isinstance(event, PullRequestEvent):
            resource_type = ResourceType.PULL_REQUEST
        else:
            raise NotImplementedError

        resources_gen = concourse_api.pipeline_resources(
            concourse_api.pipelines(),
            resource_type=resource_type,
        )

        for resource in resources_gen:
            resource: concourse.client.model.PipelineConfigResource

            ghs = resource.github_source()
            repository = event.repository()
            if not ghs.hostname() == repository.github_host():
                continue
            if not ghs.repo_path().lstrip('/') == repository.repository_path():
                continue
            if isinstance(event, PushEvent):
                if not event.ref().endswith(ghs.branch_name()):
                    continue
                if msg := event.commit_message():
                    if (
                        not ghs.disable_ci_skip()
                        and any(skip in msg for skip in ('[skip ci]', '[ci skip]'))
                    ):
                        logger.info(
                            f"Do not trigger resource {resource.name}. Found [skip ci] or [ci skip]"
                        )
                        continue

            yield resource
