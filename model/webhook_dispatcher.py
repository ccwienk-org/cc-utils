# Copyright (c) 2018 SAP SE or an SAP affiliate company. All rights reserved. This file is licensed
# under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from model.base import (
    NamedModelElement,
)


class WebhookDispatcherConfig(NamedModelElement):
    def _required_attributes(self):
        return {
            'concourse_cfgs',
        }

    def concourse_cfgs(self):
        return [
            ConcourseJobMapping(name=name, raw_dict=raw_dict) for
            name, raw_dict in self.raw['concourse_cfgs'].items()
        ]


class ConcourseJobMapping(NamedModelElement):
    def _required_attributes(self):
        return {
            'cfg_name',
            'job_mapping',
        }

    def cfg_name(self):
        return self.raw['cfg_name']

    def job_mapping(self):
        return self.raw['job_mapping']


# make backwards-compatible - XXX remove asap
WebhookDispatcher = WebhookDispatcherConfig
