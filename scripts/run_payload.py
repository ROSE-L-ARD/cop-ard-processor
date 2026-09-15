# Copyright 2026 B-Open
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from typing import Any

import eopf.triggering.workflow_types
from eopf.triggering.runner import EORunner


class CaptureCalls(object):
    calls = []

    def __init__(self, func):
        self.calls = []
        self.func = func

    def __call__(self, *args, **kwargs):
        out = self.func(*args, **kwargs)
        self.calls.append(out)
        return out


def run_payload(payload: dict[str, Any], dry_run: bool = False):
    runner = EORunner(clean=False)
    wrapped_dc = CaptureCalls(eopf.triggering.workflow_types.OutputWriteStatus)
    eopf.triggering.workflow_types.OutputWriteStatus = wrapped_dc  # type: ignore
    runner.run(payload)
    output_stores = {
        d.product_id: {"path": d.product_path, "storage_options": d.writer_params.get("storage_options", {})}
        for d in wrapped_dc.calls
    }
    return output_stores

