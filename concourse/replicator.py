import dataclasses
import enum
import os
import random
import time

from enum import Enum, IntEnum
from concurrent.futures import ThreadPoolExecutor
import functools
import logging
import textwrap
import threading
import traceback
import typing
import requests.exceptions

import mako.exceptions
import mako.template

from ci.util import (
    existing_dir,
    merge_dicts,
    urljoin,
)
from mailutil import _send_mail
import github.codeowners

import concourse.client.api
from concourse.factory import DefinitionFactory, RawPipelineDefinitionDescriptor
from concourse.enumerator import (
    DefinitionDescriptor,
    DefinitionDescriptorPreprocessor,
    TemplateRetriever,
    GithubOrganisationDefinitionEnumerator,
)

import ccc.concourse
import ccc.github
import concourse.client.model
import concourse.paths
import makoutil
import model.concourse

logger = logging.getLogger(__name__)


class RenderOrigin(enum.Enum):
    LOCAL = 'local'
    WEBHOOK_DISPATCHER = 'webhook dispatcher'
    PIPELINE_REPLICATION = 'pipeline replication'
    UNKNOWN = 'unknown'


def replicate_pipelines(
    cfg_set,
    job_mapping,
    template_path=concourse.paths.template_dir,
    template_include_dir=concourse.paths.template_include_dir,
    unpause_pipelines: bool=True,
    expose_pipelines: bool=True,
    unpause_new_pipelines: bool=True,
    remove_pipelines_filter: typing.Callable[[str], bool]=None,
):
    '''
    @param remove_pipelines_filter: pipeline-names the filter does not match are never removed
    '''

    definition_enumerators = [
        GithubOrganisationDefinitionEnumerator(
            job_mapping=job_mapping,
            cfg_set=cfg_set,
            repository_filter=lambda repo: not repo.archived, # exclude archived repositories
        ),
    ]

    preprocessor = DefinitionDescriptorPreprocessor()
    template_retriever = TemplateRetriever(template_path=template_path)
    renderer = Renderer(
        template_retriever=template_retriever,
        template_include_dir=template_include_dir,
        cfg_set=cfg_set,
        render_origin=RenderOrigin.PIPELINE_REPLICATION,
    )

    deployer = ConcourseDeployer(
        cfg_set=cfg_set,
        unpause_pipelines=unpause_pipelines,
        unpause_new_pipelines=unpause_new_pipelines,
        expose_pipelines=expose_pipelines,
    )

    result_processor = ReplicationResultProcessor(
        cfg_set=cfg_set,
        unpause_new_pipelines=unpause_new_pipelines,
        job_mapping=job_mapping,
        remove_pipelines_filter=remove_pipelines_filter,
    )

    replicator = PipelineReplicator(
        definition_enumerators=definition_enumerators,
        descriptor_preprocessor=preprocessor,
        definition_renderer=renderer,
        definition_deployer=deployer,
        result_processor=result_processor,
    )

    return replicator.replicate()


@functools.lru_cache
def _cc_utils_version():
    try:
        with open(concourse.paths.last_released_tag_file) as f:
            return f.read().strip()
    except:
        return None


class Renderer:
    def __init__(
        self,
        cfg_set,
        template_retriever: TemplateRetriever=TemplateRetriever(),
        template_include_dir=None,
        render_origin: RenderOrigin = RenderOrigin.UNKNOWN,
    ):
        self.template_retriever = template_retriever
        self.render_origin = render_origin
        if template_include_dir:
            template_include_dir = os.path.abspath(template_include_dir)
            self.template_include_dir = os.path.abspath(template_include_dir)
            from mako.lookup import TemplateLookup
            self.lookup = TemplateLookup([template_include_dir])
            self.cfg_set = cfg_set

    def render(
        self,
        definition_descriptor: DefinitionDescriptor,
    ):
        try:
            definition_descriptor = self._render(definition_descriptor)
            logger.info('rendered pipeline {pn}'.format(pn=definition_descriptor.pipeline_name))
            return RenderResult(
                definition_descriptor,
                render_status=RenderStatus.SUCCEEDED,
            )
        except Exception as e:
            logger.warning(
                f"erroneous pipeline definition '{definition_descriptor.pipeline_name}' "
                f"in repository '{definition_descriptor.main_repo.get('path')}' on branch "
                f"'{definition_descriptor.main_repo.get('branch')}'"
            )
            traceback.print_exc()
            return RenderResult(
                definition_descriptor,
                render_status=RenderStatus.FAILED,
                error_details=traceback.format_exc(),
                exception=e,
            )

    def _render(self, definition_descriptor):
        effective_definition = definition_descriptor.pipeline_definition

        # handle inheritance
        for override in definition_descriptor.override_definitions:
            effective_definition = merge_dicts(effective_definition, override)

        template_name = definition_descriptor.template_name()
        template_contents = self.template_retriever.template_contents(template_name)

        pipeline_name = definition_descriptor.pipeline_name

        pipeline_definition = RawPipelineDefinitionDescriptor(
            name=pipeline_name,
            base_definition=effective_definition.get('base_definition', {}),
            jobs=effective_definition.get('jobs', {}),
            template=template_name,
        )

        factory = DefinitionFactory(
            raw_definition_descriptor=pipeline_definition,
            cfg_set=self.cfg_set,
        )
        pipeline_metadata = {
            'definition': factory.create_pipeline_definition(),
            'name': pipeline_definition.name,
            'target_team': definition_descriptor.concourse_target_team,
            'secret_cfg': definition_descriptor.secret_cfg,
            'job_mapping': definition_descriptor.job_mapping,
            'render_origin': self.render_origin.value,
            'cc_utils_version': _cc_utils_version(),
            'pipeline_definition_committish': definition_descriptor.pipeline_definition_committish,
        }

        # also pass pipeline name if this was rendered by a replication job. Will be printed
        # in the meta-step later
        if (
            self.render_origin is RenderOrigin.PIPELINE_REPLICATION
            and (pipeline_name := os.environ.get('PIPELINE_NAME'))
        ):
            pipeline_metadata['replication_pipeline_name'] = pipeline_name

        if bg := effective_definition.get('background_image'):
            pipeline_metadata['background_image'] = bg

        for variant in pipeline_metadata.get('definition').variants():
            if not variant.has_main_repository():
                raise RuntimeError(
                    f"No main repository for pipeline definition {pipeline_definition.name}."
                )
            pipeline_metadata['pipeline_name'] = definition_descriptor.effective_pipeline_name()

        with makoutil.template_lock:
            template = mako.template.Template(template_contents, lookup=self.lookup) # nosec B702

        try:
            definition_descriptor.pipeline = template.render(
                    config_set=self.cfg_set,
                    pipeline=pipeline_metadata,
            )
        except:
            # use mako-provided function to render the stacktrace. This will properly translate
            # linenos and fill in information into the generated stacktrace
            rendered_stacktrace = mako.exceptions.text_error_template().render()
            raise RuntimeError(
                f"An exception occured when rendering pipeline '{pipeline_definition.name}': "
                f"{rendered_stacktrace}"
            ) from None

        return definition_descriptor


class RenderStatus(Enum):
    SUCCEEDED = 0
    FAILED = 1


@dataclasses.dataclass
class RenderResult:
    definition_descriptor: DefinitionDescriptor
    render_status: RenderStatus
    error_details: str = None
    exception: Exception = None


class DeployStatus(IntEnum):
    SUCCEEDED = 1
    FAILED = 2
    SKIPPED = 4
    CREATED = 8


@dataclasses.dataclass(frozen=True)
class DeployResult:
    definition_descriptor: DefinitionDescriptor
    deploy_status: DeployStatus
    error_details: str = None

    def ok(self):
        if self.deploy_status & DeployStatus.SUCCEEDED:
            return True
        elif self.deploy_status is DeployStatus.SKIPPED:
            return True
        elif self.deploy_status is DeployStatus.FAILED:
            return False
        else:
            raise NotImplementedError(self.deploy_status)


class DefinitionDeployer:
    def deploy(self, definition_descriptor):
        raise NotImplementedError('subclasses must overwrite')


class NoOpDeployer:
    def deploy(self, definition_descriptor):
        logger.info(f'Skipped deployment of pipeline {definition_descriptor.pipeline_name} (NoOp)')
        return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SUCCEEDED,
            )


class FilesystemDeployer(DefinitionDeployer):
    def __init__(self, base_dir):
        self.base_dir = existing_dir(base_dir)

    def deploy(self, definition_descriptor):
        try:
            with open(os.path.join(self.base_dir, definition_descriptor.pipeline_name), 'w') as f:
                f.write(definition_descriptor.pipeline)
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SUCCEEDED,
            )
        except Exception as e:
            logger.warning(e)
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.FAILED,
            )


class ConcourseDeployer(DefinitionDeployer):
    def __init__(
        self,
        cfg_set,
        unpause_pipelines: bool,
        unpause_new_pipelines: bool=False,
        expose_pipelines: bool=True,
    ):
        self.cfg_set = cfg_set
        self.unpause_pipelines = unpause_pipelines
        self.unpause_new_pipelines = unpause_new_pipelines
        self.expose_pipelines = expose_pipelines

    def deploy(
        self,
        definition_descriptor: DefinitionDescriptor,
    ):
        pipeline_definition = definition_descriptor.pipeline
        pipeline_name = definition_descriptor.pipeline_name
        try:
            concourse_cfg = definition_descriptor.concourse_target_cfg

            api = ccc.concourse.client_from_cfg_name(
                concourse_cfg_name=concourse_cfg.name(),
                team_name=definition_descriptor.concourse_target_team,
            )

            try:
                response = api.set_pipeline(
                    name=pipeline_name,
                    pipeline_definition=pipeline_definition
                )
            except requests.exceptions.HTTPError as e:
                # We sometimes see this non-descript error. According to an old Concourse-Issue
                # this might happen due to concurrent saves. Wait a random time and try again.
                err_content = (
                    b'failed to save config: comparison with existing config failed during save'
                )
                if e.response.status_code == 500 and e.response.content == err_content:
                    time.sleep(random.randrange(5,30))
                    response = api.set_pipeline(
                        name=pipeline_name,
                        pipeline_definition=pipeline_definition
                    )
                else:
                    raise

            logger.info(
                'Deployed pipeline: ' + pipeline_name +
                ' to team: ' + definition_descriptor.concourse_target_team
            )

            SetPipelineResult = concourse.client.model.SetPipelineResult
            if self.unpause_pipelines:
                logger.info(f'Unpausing pipeline {pipeline_name}')
                api.unpause_pipeline(pipeline_name=pipeline_name)
            elif self.unpause_new_pipelines and response is SetPipelineResult.CREATED:
                logger.info(f'Unpausing new {pipeline_name=}')
                api.unpause_pipeline(pipeline_name=pipeline_name)

            if self.expose_pipelines:
                api.expose_pipeline(pipeline_name=pipeline_name)

            deploy_status = DeployStatus.SUCCEEDED
            if response is SetPipelineResult.CREATED:
                deploy_status |= DeployStatus.CREATED
            elif response is SetPipelineResult.UPDATED:
                pass
            else:
                raise NotImplementedError

            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=deploy_status,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.warning(e)
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.FAILED,
                error_details=traceback.format_exc(),
            )


class ReplicationResultProcessor:
    def __init__(
        self,
        cfg_set,
        unpause_new_pipelines: bool=True,
        remove_pipelines: bool=True,
        remove_pipelines_filter: typing.Callable[[str], bool]=None,
        reorder_pipelines: bool=True,
        job_mapping=None,
    ):
        '''
        @param remove_pipelines_filter: pipeline-names the filter matches are never removed
        '''
        self._cfg_set = cfg_set
        self._job_mapping = job_mapping
        self.unpause_new_pipelines = unpause_new_pipelines
        self.remove_pipelines = remove_pipelines
        self.remove_pipelines_filter = remove_pipelines_filter
        self.reorder_pipelines = reorder_pipelines

        CleanupPolicy = model.concourse.PipelineCleanupPolicy
        if self._job_mapping and \
                self._job_mapping.cleanup_policy() is CleanupPolicy.NO_CLEANUP:
            self.remove_pipelines = False
            logging.info(f'{job_mapping=} will not cleanup extra pipelines due to policy')

    def process_results(self, results):
        # collect pipelines by concourse target (concourse_cfg, team_name) as key
        concourse_target_results = {}
        for result in results:
            definition_descriptor = result.definition_descriptor
            concourse_target_key = definition_descriptor.concourse_target_key()
            if concourse_target_key not in concourse_target_results:
                concourse_target_results[concourse_target_key] = []
            concourse_target_results[concourse_target_key].append(result)

        # evaluate results
        failed_descriptors = [
            d for d in results
            if not d.deploy_status & DeployStatus.SUCCEEDED
        ]
        failed_count = len(failed_descriptors)

        if failed_count > 0:
            self.remove_pipelines = False
            logger.info(
                'Failures occurred during pipeline-replication. Will not cleanup pipelines.'
            )

        for concourse_target_key, concourse_results in concourse_target_results.items():
            # TODO: implement eq for concourse_cfg
            concourse_cfg, concourse_team = next(iter(
                concourse_results)).definition_descriptor.concourse_target()

            concourse_results = concourse_target_results[concourse_target_key]
            concourse_api = ccc.concourse.client_from_cfg_name(
                concourse_cfg_name=concourse_cfg.name(),
                team_name=concourse_team,
            )

            # find pipelines to remove
            if self.remove_pipelines:
                deployed_pipeline_names = set(map(
                    lambda r: r.definition_descriptor.pipeline_name, concourse_results
                ))

                pipelines_to_remove = set(concourse_api.pipelines()) - deployed_pipeline_names

                if self.remove_pipelines_filter:
                    logger.info(f'before applying filter: {pipelines_to_remove=}')
                    pipelines_to_remove = {
                        name for name in pipelines_to_remove
                        if not self.remove_pipelines_filter(name)
                    }
                    logger.info(f'after applying filter: {pipelines_to_remove=}')

                for pipeline_name in pipelines_to_remove:
                    logger.info('removing pipeline: {p}'.format(p=pipeline_name))
                    concourse_api.delete_pipeline(pipeline_name)

            # trigger resource checks in new pipelines
            self._initialise_new_pipeline_resources(concourse_api, concourse_results)
            if self.reorder_pipelines:
                # order pipelines alphabetically
                pipeline_names = list(concourse_api.pipelines())
                pipeline_names.sort()
                concourse_api.order_pipelines(pipeline_names)

        logger.info('Successfully replicated {d} pipeline(s)'.format(d=len(results) - failed_count))

        if failed_count == 0:
            return True

        logger.warning(f'Errors occurred whilst replicating pipeline(s): {failed_count=}')

        def should_notify_pipeline_owners(deploy_result: DeployResult):
            if deploy_result.deploy_status & DeployStatus.SUCCEEDED:
                # actually, this codepath should not be hit (we filter before-hand)
                pipeline_name = deploy_result.definition_descriptor.pipeline_name
                logger.warning(f'will not notify (no err): {pipeline_name=}')
                return False

            # if one of those exceptions was raised, presumably, there was either a
            # (hopefully) transient issue (e.g. network connectivity), or a programming error
            # in our template (in which case we should not bother end-users)
            ignore_exceptions = (
                ArithmeticError,
                AttributeError,
                BufferError,
                EOFError,
                ImportError,
                MemoryError,
                NameError,
                OSError,
                ReferenceError,
                RecursionError,
                SyntaxError,
                TypeError,
            )

            if type(deploy_result.definition_descriptor.exception) in ignore_exceptions:
                return False
            else:
                return True

        all_notifications_succeeded = True
        for failed_descriptor in failed_descriptors:
            logger.warning(failed_descriptor.definition_descriptor.pipeline_name)
            failed_descriptor: DeployResult
            # failed_descriptor has type DeployResult - XXX: should rename variable
            if not should_notify_pipeline_owners(
                deploy_result=failed_descriptor,
            ):
                logger.warning(
                    'will not notify (likely the error is not on user-side '
                    f'{failed_descriptor.pipeline_name=} {failed_descriptor.error_details=}'
                )
                continue
            try:
                self._notify_broken_definition_owners(failed_descriptor)
            except Exception:
                logger.warning('an error occurred whilst trying to send error notifications')
                traceback.print_exc()
                all_notifications_succeeded = False

        # signall error only if error notifications failed
        return all_notifications_succeeded

    def _notify_broken_definition_owners(self, failed_descriptor):
        definition_descriptor = failed_descriptor.definition_descriptor
        main_repo = definition_descriptor.main_repo
        repo_owner, repo_name = main_repo['path'].split('/')
        repo_url = urljoin(main_repo['hostname'], repo_owner, repo_name)
        github_cfg = ccc.github.github_cfg_for_repo_url(
            repo_url=repo_url,
            cfg_factory=self._cfg_set,
        )
        github_api = ccc.github.github_api(github_cfg)
        repository = github_api.repository(
            repo_owner,
            repo_name,
        )

        recipients = set(github.codeowners.resolve_email_addresses(
            codeowners_entries=github.codeowners.enumerate_codeowners_from_remote_repo(
                repo=repository
            ),
            github_api=github_api,
        ))

        # in case no codeowners are available, resort to using the committer
        if not recipients:
            head_commit = repository.commit(main_repo['branch'])
            user_ids = {
                user_info.get('login')
                for user_info
                in (head_commit.committer, head_commit.author)
                if user_info and user_info.get('login')
            }
            for user_id in user_ids:
                user = github_api.user(user_id)
                if user.email:
                    recipients.add(user.email)

        # if there are still no recipients available print a warning
        if not recipients:
            logger.warning(textwrap.dedent(
                f"""
                Unable to determine recipient for pipeline '{definition_descriptor.pipeline_name}'
                found in branch '{main_repo['branch']}' ({main_repo['path']}). Please make sure that
                CODEOWNERS and committers have exposed a public e-mail address in their profile.
                """
            ))
        else:
            logger.info(f'Sending notification e-mail to {recipients} ({main_repo["path"]})')
            email_cfg = self._cfg_set.email()
            _send_mail(
                email_cfg=email_cfg,
                recipients=recipients,
                subject='Your pipeline definition in {repo} is erroneous'.format(
                    repo=main_repo['path'],
                ),
                mail_template=textwrap.dedent(
                f'''
                    The pipeline definition for {definition_descriptor.pipeline_name=}
                    on {main_repo["branch"]=} failed to be rendered.
                    Error details:
                    {str(failed_descriptor.error_details)}
                '''
                ),
            )

    def _initialise_new_pipeline_resources(
        self,
        concourse_api: concourse.client.api.ConcourseApiBase,
        results,
    ):
        newly_deployed_pipeline_names = [
            result.definition_descriptor.pipeline_name for result in results
            if result.deploy_status & DeployStatus.CREATED
        ]

        for pipeline_name in newly_deployed_pipeline_names:
            if self.unpause_new_pipelines:
                logger.info(f'unpausing new {pipeline_name=}')
                concourse_api.unpause_pipeline(pipeline_name)

            logger.info(f'triggering initial resource check for {pipeline_name=}')

            for pipeline_config_resource in concourse_api.pipeline_resources(pipeline_name):
                pipeline_config_resource: concourse.client.api.PipelineConfigResource
                concourse_api.trigger_resource_check(
                    pipeline_name=pipeline_name,
                    resource_name=pipeline_config_resource.name,
                )


class PipelineValidationError(Exception):
    pass


class PipelineValidationResultProcessor:
    def process_results(self, results: typing.Iterable[DeployResult]):
        failed_results = [
            r for r in results if not r.deploy_status is DeployStatus.SUCCEEDED
        ]
        if failed_results:
            raise PipelineValidationError(
                '\n'.join((
                    f'{r.definition_descriptor.pipeline_name}: {r.error_details}'
                    for r in failed_results)
                )
            )

        return True


class PipelineReplicator:
    def __init__(
            self,
            definition_enumerators,
            descriptor_preprocessor,
            definition_renderer,
            definition_deployer,
            result_processor=None,
        ):
        self.definition_enumerators = definition_enumerators
        self.descriptor_preprocessor = descriptor_preprocessor
        self.definition_renderer = definition_renderer
        self.definition_deployer = definition_deployer
        self.result_processor = result_processor

        # keep track of generated pipelines to detect conflicts
        self._pipeline_names_lock = threading.Lock()
        self._pipeline_names = set()

    def _pipeline_name_conflict(self, definition_descriptor:DefinitionDescriptor):
        with self._pipeline_names_lock:
            pipeline_name = definition_descriptor.pipeline_name
            if pipeline_name in self._pipeline_names:
                return True
            self._pipeline_names.add(pipeline_name)

    def _enumerate_definitions(self):
        for enumerator in self.definition_enumerators:
            yield from enumerator.enumerate_definition_descriptors()

    def _process_definition_descriptor(self, definition_descriptor):
        if definition_descriptor.exception:
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SKIPPED,
                error_details=definition_descriptor.exception,
            )

        preprocessed = self.descriptor_preprocessor.process_definition_descriptor(
                definition_descriptor
        )
        result = self.definition_renderer.render(preprocessed)

        if self._pipeline_name_conflict(
            definition_descriptor=result.definition_descriptor,
        ):
            # early exit upon pipeline name conflict
            pipeline_name = result.definition_descriptor.pipeline_name
            logger.warning(f'duplicate pipeline name: {pipeline_name}')
            return DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SKIPPED,
                error_details=f'duplicate pipeline name: {pipeline_name}',
            )

        if result.render_status == RenderStatus.SUCCEEDED:
            deploy_result = self.definition_deployer.deploy(result.definition_descriptor)
        else:
            deploy_result = DeployResult(
                definition_descriptor=definition_descriptor,
                deploy_status=DeployStatus.SKIPPED,
                error_details=result.error_details,
            )
        return deploy_result

    def _replicate(self):
        executor = ThreadPoolExecutor(max_workers=16)
        yield from executor.map(
            self._process_definition_descriptor,
            self._enumerate_definitions(),
        )

    def replicate(self):
        results = [
            result for result in self._replicate()
        ]

        if self.result_processor:
            return self.result_processor.process_results(results)
        else:
            return results
