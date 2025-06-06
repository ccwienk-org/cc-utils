# SPDX-FileCopyrightText: 2024 SAP SE or an SAP affiliate company and Gardener contributors
#
# SPDX-License-Identifier: Apache-2.0

import logging

import git
import smtplib
import typing

import ocm
import ocm.util

from model.email import EmailConfig
import ci.log
from ci.util import (
    existing_dir,
    not_empty,
    not_none,
    info,
    fail,
    ctx,
)
from mail import template_mailer as mailer
import ccc.github
import github.codeowners
import version


ci.log.configure_default_logging()
logger = logging.getLogger(__name__)


def send_mail(
    email_cfg_name: str,
    recipients: typing.Iterable[str],
    mail_template_file: str,
    subject: str,
    cc_recipients: typing.Iterable[str],
    replace_token: typing.Iterable[str],
):
    '''
    Sends an email using the specified email_cfg (retrieved from a cfg_factory) to the specified
    recipients. The mail body is read from a file. A simple token-replacement is done if
    (optional) replace-tokens are given.

    @param recipients: mail recipients (email addresses)
    @param mail_template_file: path to the mail template file. Must exist.
    @param subject: email subject
    @param cc_recipients: cc mail recipients
    @param replace_token: format: <token>=<replace-value> - tokens in mail-body are replaced
    '''
    not_empty(email_cfg_name)

    cfg_factory = ctx().cfg_factory()
    email_cfg = cfg_factory.email(email_cfg_name)

    with open(mail_template_file) as f:
        mail_template = f.read()

    # validate template-tokens
    invalid_tokens = filter(lambda t: not isinstance(t, str) or '=' not in t, replace_token)
    if len(list(invalid_tokens)) > 0:
        fail('all replace-tokens must be of form <key>=<value>: ' + ' '.join(
            invalid_tokens
        )
        )

    # parse replace-tokens
    replace_tokens = dict(map(lambda t: t.split('=', 1), replace_token))

    _send_mail(
        email_cfg=email_cfg,
        recipients=recipients,
        mail_template=mail_template,
        subject=subject,
        cc_recipients=cc_recipients,
        replace_tokens=replace_tokens,
    )


def _send_mail(
    email_cfg: EmailConfig,
    recipients: typing.Iterable[str],
    mail_template: str,
    subject: str,
    replace_tokens: dict={},
    cc_recipients: typing.Iterable[str]=[],
    mimetype='text',
):
    not_none(email_cfg)
    not_empty(recipients)
    not_none(mail_template)
    not_empty(subject)

    # create body from template
    mail_body = mailer.create_body(
        mail_template=mail_template,
        replace_tokens=replace_tokens,
    )

    recipients = {r.lower() for r in recipients}
    cc_recipients = {r.lower() for r in cc_recipients}

    sender_name = email_cfg.sender_name()

    if email_cfg.use_tls():
        smtp_server = smtplib.SMTP_SSL(email_cfg.smtp_host())
    else:
        smtp_server = smtplib.SMTP(email_cfg.smtp_host())

    if email_cfg.has_credentials():
        credentials = email_cfg.credentials()
        smtp_server.login(user=credentials.username(), password=credentials.passwd())

    # create mail envelope
    mail = mailer.create_mail(
        subject=subject,
        sender=sender_name,
        recipients=recipients,
        cc_recipients=cc_recipients,
        text=mail_body,
        mimetype=mimetype,
    )

    recipients.update(cc_recipients)
    recipients = email_cfg.filter_recipients(recipients)
    recipients = list(recipients) # ensure it is a container

    if len(recipients) > 50:
        logger.warning('max recipients exceeded, will limit to 50')
        recipients = recipients[:50]

    smtp_server.send_message(msg=mail, to_addrs=recipients)  # from_addr is taken from header


#TODO: refactor into class - MailHelper?
def determine_head_commit_recipients(
    src_dirs=(),
):
    '''returns a generator yielding e-mail adresses from the head commit's author and
    committer for all given repository work trees.
    '''
    for src_dir in src_dirs:
        # commiter/author from head commit
        repo = git.Repo(existing_dir(src_dir))
        head_commit = repo.commit(repo.head)
        yield head_commit.author.email.lower()
        yield head_commit.committer.email.lower()


def determine_local_repository_codeowners_recipients(
    github_api,
    src_dirs=(),
):
    '''returns a generator yielding e-mail adresses from all given repository work
    tree's CODEOWNERS files.
    '''
    def enumerate_entries_from_src_dirs(src_dirs):
        for src_dir in src_dirs:
            yield from github.codeowners.enumerate_codeowners_from_local_repo(
                repo_dir=src_dir,
            )

    entries = enumerate_entries_from_src_dirs(src_dirs)

    yield from github.codeowners.resolve_email_addresses(
        codeowners_entries=entries,
        github_api=github_api,
    )


def determine_codeowner_file_recipients(
    github_api,
    codeowners_files=(),
):
    '''returns a generator yielding e-mail adresses from the given CODEOWNERS file(s).
    '''
    def enumerate_entries_from_codeowners_files(codeowners_files):
        for codeowners_file in codeowners_files:
            yield from github.codeowners.enumerate_codeowners_from_file(codeowners_file)

    entries = enumerate_entries_from_codeowners_files(codeowners_files)
    yield from github.codeowners.resolve_email_addresses(
        codeowners_entries=entries,
        github_api=github_api,
    )


def determine_mail_recipients(
    github_cfg_name,
    src_dirs=(),
    components: typing.Sequence[ocm.Component]=(),
    component_names=(),
    codeowners_files=(),
    branch_name='master',
    component_descriptor_lookup=None,
    version_lookup=None,
):
    '''
    returns a generator yielding all email addresses for the given (git) repository work tree
    Email addresses are looked up:
    - from head commit: author and committer
    - from *CODEOWNERS files [0]

    Email addresses are not de-duplicated (this should be done by consumers)

    [0] https://help.github.com/articles/about-codeowners/
    '''
    if not any((components, component_names, src_dirs, codeowners_files)):
        return # nothing to do

    if components and component_names:
        raise ValueError('only one of components, component_names must be set')

    if component_names and not component_descriptor_lookup:
        raise ValueError(
            'If component_names is given, component_descriptor_lookup must also be given'
        )

    cfg_factory = ctx().cfg_factory()

    github_cfg = cfg_factory.github(github_cfg_name)
    github_api = ccc.github.github_api(github_cfg)

    yield from determine_head_commit_recipients(src_dirs)

    yield from determine_local_repository_codeowners_recipients(
        github_api=github_api,
        src_dirs=src_dirs,
    )

    yield from determine_codeowner_file_recipients(
        github_api=github_api,
        codeowners_files=codeowners_files,
    )

    if component_names:
        entries_and_apis = [
            _codeowners_parser_from_component_name(
                component_name=component_name,
                component_descriptor_lookup=component_descriptor_lookup,
                version_lookup=version_lookup,
                branch_name=branch_name
            ) for component_name in component_names
        ]
    elif components:
        entries_and_apis = [
            _codeowners_parser_from_component(
                component=component,
                branch_name=branch_name
            ) for component in components
        ]
    else:
        raise ValueError('One of components and component_names must be given')

    for api, codeowner_entries in entries_and_apis:
        yield from github.codeowners.resolve_email_addresses(
            codeowners_entries=codeowner_entries,
            github_api=api,
        )


def _codeowners_parser_from_component_name(
    component_name: str,
    component_descriptor_lookup,
    version_lookup,
    branch_name='master',
):
    greatest_version = version.greatest_version(
        versions=version_lookup(component_name)
    )

    component = component_descriptor_lookup((
        component_name,
        greatest_version,
    ))
    return _codeowners_parser_from_component(
        component=component,
        branch_name=branch_name,
    )


def _codeowners_parser_from_component(
    component: ocm.Component,
    branch_name: str='master',
):
    main_source = ocm.util.main_source(
        component=component,
        no_source_ok=False,
    )
    if not main_source.access.type is ocm.AccessType.GITHUB:
        raise NotImplementedError(main_source.access.type)

    access = main_source.access
    github_api = ccc.github.github_api_from_gh_access(access=access)

    repository = github_api.repository(
        access.org_name(),
        access.repository_name(),
    )

    return github_api, github.codeowners.enumerate_codeowners_from_remote_repo(
        repo=repository,
    )


def notify(
    subject: str,
    body: str,
    email_cfg_name: str,
    recipients: typing.Iterable[str],
):
    recipients = set(recipients)
    cfg_factory = ctx().cfg_factory()

    email_cfg = cfg_factory.email(email_cfg_name)

    _send_mail(
        email_cfg=email_cfg,
        recipients=recipients,
        mail_template=body,
        subject=subject
    )
    info('sent email to: {r}'.format(r=recipients))
