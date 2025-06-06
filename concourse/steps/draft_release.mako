<%def
  name="draft_release_step(job_step, job_variant, github_cfg, indent)",
  filter="indent_func(indent),trim">
<%
from makoutil import indent_func
import os
import concourse.steps.component_descriptor_util as cdu
import ocm
version_file = job_step.input('version_path') + '/version'
repo = job_variant.main_repository()
draft_release_trait = job_variant.trait('draft_release')
component_descriptor_trait = job_variant.trait('component_descriptor')
component_name = component_descriptor_trait.component_name()
version_operation = draft_release_trait._preprocess()
component_descriptor_path = os.path.join(
    job_step.input('component_descriptor_dir'),
    cdu.component_descriptor_fname(ocm.SchemaVersion.V2),
)
%>
import logging
import os
import version

import ccc.delivery
import ccc.github
import ccc.oci
import ci.log
import ci.util
import cnudie.retrieve
import cnudie.util
import github.release
import gitutil
import ocm
import release_notes.fetch
import release_notes.markdown


logger = logging.getLogger('draft-release')
ci.log.configure_default_logging()

if '${version_operation}' != 'finalize':
    raise NotImplementedError(
        "Version-processing other than 'finalize' is not supported for draft release creation"
    )

with open('${version_file}') as f:
  version_str = f.read().strip()

processed_version = version.process_version(
    version_str=version_str,
    operation='${version_operation}',
)

repo_dir = ci.util.existing_dir('${repo.resource_name()}')

if not os.path.exists(component_descriptor_path := '${component_descriptor_path}'):
   logger.error(f'did not find component-descriptor at {component_descriptor_path}')
   exit(1)

component = ocm.ComponentDescriptor.from_dict(
        component_descriptor_dict=ci.util.parse_yaml_file(
            component_descriptor_path,
        ),
        validation_mode=ocm.ValidationMode.WARN,
).component

github_cfg = ccc.github.github_cfg_for_repo_url(
  ci.util.urljoin(
    '${repo.repo_hostname()}',
    '${repo.repo_path()}'
  ),
)

<%
import concourse.steps
template = concourse.steps.step_template('component_descriptor')
ocm_repository_lookup = template.get_def('ocm_repository_lookup').render
%>
${ocm_repository_lookup(component_descriptor_trait.ocm_repository_mappings())}

oci_client = ccc.oci.oci_client()
component_descriptor_lookup = cnudie.retrieve.create_default_component_descriptor_lookup(
    ocm_repository_lookup=ocm_repository_lookup,
    oci_client=oci_client,
    delivery_client=ccc.delivery.default_client_if_available(),
)
ocm_version_lookup = cnudie.retrieve.version_lookup(
    ocm_repository_lookup=ocm_repository_lookup,
    oci_client=oci_client,
)

github_api = ccc.github.github_api(github_cfg)
repository = github_api.repository(
    '${repo.repo_owner()}',
    '${repo.repo_name()}',
)

git_helper = gitutil.GitHelper(
    repo=repo_dir,
    git_cfg=github_cfg.git_cfg(
        repo_path='${repo.repo_owner()}/${repo.repo_name()}',
    ),
)
try:
    release_note_blocks = release_notes.fetch.fetch_draft_release_notes(
        component=component,
        component_descriptor_lookup=component_descriptor_lookup,
        version_lookup=ocm_version_lookup,
        git_helper=git_helper,
        github_api_lookup=ccc.github.github_api_lookup,
        version_whither=version_str,
    )
    release_notes_md = '\n'.join(
        str(i) for i in release_notes.markdown.render(release_note_blocks)
    ) or 'no release notes available'
except ValueError as e:
    logger.warning(f'Error when computing release notes: {e}')
    # this will happen if a component-descriptor for a more recent version than what is available in the
    # repository is already published - usually by steps that erroneously publish them before they should.
    release_notes_md = 'no release notes available'

draft_name = f'{processed_version}-draft'
draft_release = github.release.find_draft_release(
    repository=repository,
    name=draft_name,
)
body, _ = github.release.body_or_replacement(
    body=release_notes_md,
)
if not draft_release:
    logger.info(f"Creating {draft_name=}")
    repository.create_release(
        tag_name=draft_name,
        name=draft_name,
        body=body,
        draft=True,
        prerelease=False,
    )
else:
    if not draft_release.body == body:
        logger.info(f"Updating draft-release '{draft_name}'")
        draft_release.edit(body=body)
    else:
        logger.info('draft release notes are already up to date')

logger.info("Checking for outdated draft releases to delete")
for release, deletion_successful in github.release.delete_outdated_draft_releases(repository):
    if deletion_successful:
        logger.info(f"Deleted release '{release.name}'")
    else:
        logger.warning(f"Could not delete release '{release.name}'")
</%def>
