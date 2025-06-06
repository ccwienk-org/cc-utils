<%def
  name="component_descriptor_step(job_step, job_variant, output_image_descriptors, indent)",
  filter="indent_func(indent),trim"
>
<%
import collections
import dataclasses
from makoutil import indent_func
from concourse.steps import step_lib
import ocm
import concourse.model.traits.component_descriptor as comp_descr_trait

if job_variant.has_trait('publish'):
  publish_trait = job_variant.trait('publish')
  helmcharts = publish_trait.helmcharts
else:
  helmcharts = ()
descriptor_trait = job_variant.trait('component_descriptor')
main_repo = job_variant.main_repository()
main_repo_labels = main_repo.source_labels()
main_repo_path_env_var = main_repo.logical_name().replace('-', '_').upper() + '_PATH'
other_repos = [r for r in job_variant.repositories() if not r.is_main_repo()]
if descriptor_trait.ocm_repository:
  ocm_repository_url = descriptor_trait.ocm_repository.oci_ref
else:
  ocm_repository_url = None
retention_policy = descriptor_trait.retention_policy()
ocm_repository_mappings = descriptor_trait.ocm_repository_mappings()

# label main repo as main
if not 'cloud.gardener/cicd/source' in [label.name for label in main_repo_labels]:
  main_repo_labels.append(
    ocm.Label(
      name='cloud.gardener/cicd/source',
      value={'repository-classification': 'main'},
    )
  )

# group images by _base_name (deduplicate platform-variants)
images_by_base_name = {}
for image_descriptor in output_image_descriptors.values():
  if image_descriptor._base_name in images_by_base_name:
    continue
  images_by_base_name[image_descriptor._base_name] = image_descriptor
%>
import dataclasses
import enum
import json
import logging
import os
import pprint
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback

import dacite
import git
import yaml

import ccc.delivery
import ccc.oci
import cnudie.purge
import cnudie.retrieve
import cnudie.util
import ocm
import ocm.upload
import oci.auth as oa
import version
# required for deserializing labels
Label = ocm.Label

from ci.util import fail, parse_yaml_file, ctx

logger = logging.getLogger('step.component_descriptor')

${step_lib('component_descriptor')}
${step_lib('component_descriptor_util')}

# retrieve effective version
version_file_path = os.path.join(
  '${job_step.input('version_path')}',
  'version',
)
with open(version_file_path) as f:
  effective_version = f.read().strip()

component_name = '${descriptor_trait.component_name()}'
component_labels = ${descriptor_trait.component_labels()}
component_name_v2 = component_name.lower() # OCI demands lowercase
ocm_repository_url = '${ocm_repository_url}'

oci_client = ccc.oci.oci_client()

${ocm_repository_lookup(ocm_repository_mappings)}
component_descriptor_lookup = cnudie.retrieve.create_default_component_descriptor_lookup(
  ocm_repository_lookup=ocm_repository_lookup,
  oci_client=oci_client,
  delivery_client=ccc.delivery.default_client_if_available(),
)
version_lookup = cnudie.retrieve.version_lookup(
  ocm_repository_lookup=ocm_repository_lookup,
  oci_client=oci_client,
)

main_repo_path = os.path.abspath('${main_repo.resource_name()}')
commit_hash = head_commit_hexsha(main_repo_path)

main_repo_url = '${main_repo.repo_hostname()}/${main_repo.repo_path()}'

# create base descriptor filled with default values
base_descriptor_v2 = base_component_descriptor_v2(
    component_name_v2=component_name_v2,
    component_labels=component_labels,
    effective_version=effective_version,
    source_labels=${[dataclasses.asdict(label) for label in main_repo_labels]},
    ocm_repository_url=ocm_repository_url,
    commit=commit_hash,
    repo_url=main_repo_url,
)
component_v2 = base_descriptor_v2.component

## XXX unify w/ injection-method used for main-repository
% for repository in other_repos:
repo_labels = ${repository.source_labels()}
if not 'cloud.gardener/cicd/source' in [label.name for label in repo_labels]:
  repo_labels.append(
    ocm.Label(
      name='cloud.gardener/cicd/source',
      value={'repository-classification': 'auxiliary'},
    ),
  )

if not (repo_commit_hash := head_commit_hexsha(os.path.abspath('${repository.resource_name()}'))):
    logger.warning('Could not determine commit hash')

component_v2.sources.append(
    ocm.Source(
        name='${repository.logical_name().replace('/', '_').replace('.', '_')}',
        type=ocm.ArtefactType.GIT,
        access=ocm.GithubAccess(
            type=ocm.AccessType.GITHUB,
            repoUrl='${repository.repo_hostname()}/${repository.repo_path()}',
            ref='${repository.branch()}',
            commit=repo_commit_hash,
        ),
        version=effective_version,
        labels=repo_labels,
    )
)
% endfor

# add container image references (from publish-trait)
% for name, image_descriptor in images_by_base_name.items():
<%
  target_names = set()
%>
%   for target_spec in image_descriptor.targets:
<%
  if target_spec.name in target_names:
    continue
  target_names.add(target_spec.name)
%>
component_v2.resources.append(
  ocm.Resource(
    name='${target_spec.name}',
    version=effective_version, # always inherited from component
    type=ocm.ArtefactType.OCI_IMAGE,
    relation=ocm.ResourceRelation.LOCAL,
    access=ocm.OciAccess(
      type=ocm.AccessType.OCI_REGISTRY,
      imageReference='${target_spec.image}' + ':' + effective_version,
    ),
    labels=${image_descriptor.resource_labels()},
    extraIdentity={
      'version': effective_version,
    },
  ),
)
%   endfor
% endfor

# add helmcharts (from publish-trait)
% for helmchart in helmcharts:
<%
name = helmchart.name
target_ref_prefix = f'{helmchart.registry}/{name}'
%>
component_v2.resources.append(
  ocm.Resource(
    name='${helmchart.name}',
    version=effective_version, # always inherited from component
    type='helmChart',
    extraIdentity={
      'type': 'helmChart', # allow images w/ same name
    },
    relation=ocm.ResourceRelation.LOCAL,
    access=ocm.OciAccess(
      type=ocm.AccessType.OCI_REGISTRY,
      imageReference=f'${target_ref_prefix}:{effective_version}',
    ),
  )
)
% endfor

logger.info('default component descriptor:\n')
print(dump_component_descriptor_v2(base_descriptor_v2))
print('\n' * 2)

descriptor_out_dir = os.path.abspath('${job_step.output("component_descriptor_dir")}')

v2_outfile = os.path.join(
  descriptor_out_dir,
  component_descriptor_fname(schema_version=ocm.SchemaVersion.V2),
)

descriptor_script = os.path.abspath(
  '${job_variant.main_repository().resource_name()}/.ci/component_descriptor'
)
if os.path.isfile(descriptor_script):
  is_executable = bool(os.stat(descriptor_script)[stat.ST_MODE] & stat.S_IEXEC)
  if not is_executable:
    fail(f'descriptor script file exists but is not executable: {descriptor_script}')

  # dump base_descriptor_v2 and pass it to descriptor script
  base_component_descriptor_fname = (
    f'base_{component_descriptor_fname(schema_version=ocm.SchemaVersion.V2)}'
  )
  base_descriptor_file_v2 = os.path.join(
    descriptor_out_dir,
    base_component_descriptor_fname,
  )
  with open(base_descriptor_file_v2, 'w') as f:
    f.write(dump_component_descriptor_v2(base_descriptor_v2))

  subproc_env = os.environ.copy()
  subproc_env['${main_repo_path_env_var}'] = main_repo_path
  subproc_env['MAIN_REPO_DIR'] = main_repo_path
  subproc_env['BASE_DEFINITION_PATH'] = base_descriptor_file_v2
  subproc_env['COMPONENT_DESCRIPTOR_PATH'] = v2_outfile
  subproc_env['COMPONENT_NAME'] = component_name
  subproc_env['COMPONENT_VERSION'] = effective_version
  subproc_env['EFFECTIVE_VERSION'] = effective_version
  subproc_env['CURRENT_COMPONENT_REPOSITORY'] = ocm_repository_url

  # pass predefined command to add dependencies for convenience purposes
  add_dependencies_cmd = ' '.join((
    'gardener-ci',
    'productutil_v2',
    'add_dependencies',
    '--descriptor-src-file', base_descriptor_file_v2,
    '--descriptor-out-file', base_descriptor_file_v2,
    '--component-version', effective_version,
    '--component-name', component_name,
  ))

  subproc_env['ADD_DEPENDENCIES_CMD'] = add_dependencies_cmd

  % for name, value in descriptor_trait.callback_env().items():
  subproc_env['${name}'] = '${value}'
  % endfor

  subprocess.run(
    [descriptor_script],
    check=True,
    cwd=descriptor_out_dir,
    env=subproc_env
  )

else:
  logger.info(
    f'no component_descriptor script found at {descriptor_script} - will use default'
  )
  with open(v2_outfile, 'w') as f:
    f.write(dump_component_descriptor_v2(base_descriptor_v2))
  logger.info(f'wrote OCM component descriptor: {v2_outfile=}')

have_cd = os.path.exists(v2_outfile)

if have_cd:
  # ensure the script actually created an output
  if not os.path.isfile(v2_outfile):
    fail(f'no descriptor file was found at: {v2_outfile=}')

  descriptor_v2 = ocm.ComponentDescriptor.from_dict(
    ci.util.parse_yaml_file(v2_outfile)
  )
  logger.info(f'found component-descriptor (v2) at {v2_outfile=}:\n')
  print(dump_component_descriptor_v2(descriptor_v2))
else:
  print(f'XXX: did not find a component-descriptor at {v2_outfile=}')
  exit(1)

% if descriptor_trait.upload is comp_descr_trait.UploadMode.LEGACY:
  % if not (job_variant.has_trait('release') or job_variant.has_trait('update_component_deps')):
if descriptor_v2 and ocm_repository_url:
  ocm_repository = ocm.OciOcmRepository(baseUrl=ocm_repository_url)

  if descriptor_v2.component.current_ocm_repo != ocm_repository:
    descriptor_v2.component.repositoryContexts.append(ocm_repository)

  target_ref = cnudie.util.oci_artefact_reference(descriptor_v2.component)

  ocm.upload.upload_component_descriptor(
    component_descriptor=descriptor_v2,
    ocm_repository=ocm_repository,
    oci_client=oci_client,
  )
  logger.info(f'uploaded component-descriptor to {target_ref}')
  % endif
% endif

# determine "bom-diff" (changed component references)
try:
  bom_diff = component_diff_since_last_release(
      component_descriptor=descriptor_v2,
      component_descriptor_lookup=component_descriptor_lookup,
      version_lookup=version_lookup,
  )
except:
  logger.warning('failed to determine component-diff')
  import traceback
  traceback.print_exc()
  bom_diff = None

if not bom_diff:
  logger.info('no differences in referenced components found since last release')
else:
  logger.info('component dependencies diff was written to dependencies.diff')
  dependencies_path = os.path.join(descriptor_out_dir, 'dependencies.diff')
  write_component_diff(
    component_diff=bom_diff,
    out_path=dependencies_path,
  )
  with open(dependencies_path) as f:
    print(f.read())
% if retention_policy:

logger.info('will honour retention-policy')
retention_policy = dacite.from_dict(
  data_class=version.VersionRetentionPolicies,
  data=${retention_policy},
  config=dacite.Config(cast=(enum.Enum,)),
)
pprint.pprint(retention_policy)

if retention_policy.dry_run:
  logger.info('dry-run - will only print versions to remove, but not actually remove them')
else:
  logger.info('!! will attempt to remove listed component-versions, according to policy')

logger.info('the following versions were identified for being purged')
component = descriptor_v2.component


for idx, component_id in enumerate(cnudie.purge.iter_componentversions_to_purge(
    component=component,
    policy=retention_policy,
    oci_client=oci_client,
)):
  if idx >= 64:
    print('will abort the purge, considering there seem to be more than 64 versions to cleanup')
    print('this is done to limit execution-time - the purge will continue on next execution')
    exit(0)
  print(f'{idx} {component_id.name}:{component_id.version}')
  if retention_policy.dry_run:
   continue
  component_to_purge = component_descriptor_lookup(
    ocm.ComponentIdentity(
      name=component.name,
      version=component_id.version,
    )
  )
  if not component_to_purge:
    logger.warning(f'{component.name}:{component_id.version} was not found - ignore')
    continue

  try:
   cnudie.purge.remove_component_descriptor_and_referenced_artefacts(
    component=component_to_purge,
    oci_client=oci_client,
    lookup=component_descriptor_lookup,
    recursive=False,
   )
  except Exception as e:
   logger.warning(f'error occurred while trying to purge {component_id}: {e}')
   traceback.print_exc()
% else:
logger.info('no retention-policy was defined - will not purge component-descriptors')
% endif
</%def>

<%def
  name="ocm_repository_lookup(ocm_repository_mappings)",
  filter="trim"
>
<%
'''
generates a function `ocm_repository_lookup`; handy for being used in cnudie.retrieve
'''
import cnudie.retrieve
OcmRepositoryMappingEntry = cnudie.retrieve.OcmRepositoryMappingEntry
for mapping in ocm_repository_mappings:
  if not isinstance(mapping, OcmRepositoryMappingEntry):
    raise ValueError(mapping)
%>
import oci.model as om
import ocm
import cnudie.util
def ocm_repository_lookup(component: ocm.ComponentIdentity, /):
% if not ocm_repository_mappings:
  return
% endif
% for mapping in ocm_repository_mappings:
  % if not mapping.prefix:
  yield '${mapping.repository}'
  % else:
  component_name = cnudie.util.to_component_name(component)
  if component_name.startswith('${mapping.prefix}'):
    yield '${mapping.repository}'
  % endif
% endfor

</%def>
