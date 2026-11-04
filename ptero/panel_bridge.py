"""Invoke supported Laravel contracts with JSON through stdin, never shell interpolation."""

import json
import tempfile
from .core import PROJECT, InstallerError


def execute(installer, code, data):
    panel = installer.panel_state()
    if panel.get('method', 'docker') == 'standalone':
        backend = installer.native(panel)
        prefix, app = ['runuser', '-u', 'www-data', '--', backend.php], str(backend.app)
    else:
        prefix = ['docker', 'compose', '--project-name', PROJECT, '--file', installer.p.compose, 'exec', '-T', 'panel', 'php']
        app = '/app'
    script = (
        f'require {json.dumps(app + "/vendor/autoload.php")};'
        f'$app=require {json.dumps(app + "/bootstrap/app.php")};'
        '$app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();'
        '$data=json_decode(stream_get_contents(STDIN),true,512,JSON_THROW_ON_ERROR);' + code
    )
    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(data).encode())
        stream.seek(0)
        _, output = installer.run.run(
            [*prefix, '-r', script],
            input_file=stream,
            label='Updating Panel resources through supported contracts',
            cwd=app if panel.get('method') == 'standalone' else None,
        )
    try:
        return json.loads(output)
    except (ValueError, TypeError) as exc:
        raise InstallerError('The Panel returned an invalid resource response.') from exc
