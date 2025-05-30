# SPDX-FileCopyrightText: 2024 SAP SE or an SAP affiliate company and Gardener contributors
#
# SPDX-License-Identifier: Apache-2.0


import logging

from model.webhook_dispatcher import WebhookDispatcherConfig
from .dispatcher import GithubWebhookDispatcher
from .model import CreateEvent, PushEvent, PullRequestEvent

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class GithubWebhook:
    def __init__(
        self,
        cfg_factory,
        cfg_set,
        whd_cfg: WebhookDispatcherConfig,
    ):
        self.dispatcher = GithubWebhookDispatcher(
            cfg_factory=cfg_factory,
            cfg_set=cfg_set,
            whd_cfg=whd_cfg,
        )

    def on_post(self, req, resp):
        event = req.get_header('X-GitHub-Event', required=True)
        delivery = req.get_header('X-GitHub-Delivery', required=True)
        logger_string = f'received event (delivery-id: {delivery}) of type "{event}"'
        action = req.media.get("action")
        if action:
            logger_string += f' with action "{action}"'
        repository_name = req.media.get("repository", {}).get("full_name")
        # no GHES header ^= github.com
        hostname = req.get_header('X-GitHub-Enterprise-Host') or 'github.com'
        if repository_name:
            logger_string += f' for repository "{repository_name}"'
        if hostname:
            logger_string += f' from "{hostname}"'

        logger.info(logger_string)
        if event == 'push':
            parsed = PushEvent(raw_dict=req.media, delivery=delivery, hostname=hostname)
            self.dispatcher.dispatch_push_event(
                push_event=parsed,
            )
            return
        if event == 'create':
            parsed = CreateEvent(raw_dict=req.media, delivery=delivery, hostname=hostname)
            self.dispatcher.dispatch_create_event(create_event=parsed)
            return
        elif event == 'pull_request':
            parsed = PullRequestEvent(raw_dict=req.media, delivery=delivery, hostname=hostname)
            processing = self.dispatcher.dispatch_pullrequest_event(
                pr_event=parsed,
            )
            if not processing:
                resp.text = "Event ignored"
            return
        else:
            msg = f'event {event} ignored'
            logger.info(msg)
            return
