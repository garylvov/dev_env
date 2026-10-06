"""Generate a native-client escape hatch without loading launcher hooks."""
from __future__ import annotations
import json
from .simple_types import LaunchOptions, TaskView


def native_recipe(view: TaskView, options: LaunchOptions, agent: str = 'coordinator') -> dict:
    from .task_files import recovery_input, has_work_context
    argv = [options.executable or options.engine]
    if options.engine == 'codex':
        argv += ['--cd', str(view.workspace)]
        if options.yolo:
            argv += ['--dangerously-bypass-approvals-and-sandbox']
        if options.effort:
            argv += ['-c', 'model_reasoning_effort=' + json.dumps(options.effort)]
    else:
        if options.yolo:
            argv += ['--dangerously-skip-permissions']
        if options.effort:
            argv += ['--effort', options.effort]
    if options.model:
        argv += ['--model', options.model]
    argv += ['--add-dir', str(view.root)]
    if has_work_context(view, agent=agent):
        from .simple_guidance import TASK_RULES
        argv += ['--', TASK_RULES + '\n\n' + recovery_input(view, agent=agent, paths_only=True).text]
    return {'argv': argv, 'cwd': str(view.workspace) if view.workspace else None,
            'assignment': str(view.assignment), 'state': str(view.state),
            'note': 'Native continuation only, without automatic Token Kit rollover. '
                    'End any managed client for this assignment before using this recipe.'}
