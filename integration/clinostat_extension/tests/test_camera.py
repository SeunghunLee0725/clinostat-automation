from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK_CAMERA = ROOT / 'work' / 'camera.py'
DEPLOY_SCRIPT = ROOT.parents[0] / 'deploy' / 'deploy_to_pi.sh'


def test_camera_module_is_canonical_and_deployed():
    assert WORK_CAMERA.is_file()

    deploy_script = DEPLOY_SCRIPT.read_text()
    assert 'clinostat_extension/work/camera.py:$CLINOSTAT_ROOT/camera.py' in deploy_script
    assert (
        'cp -a $BACKUP_ROOT/clinostat/camera.py $CLINOSTAT_ROOT/camera.py'
        in deploy_script
    )
