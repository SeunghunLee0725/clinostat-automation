from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK_CAMERA = ROOT / 'work' / 'camera.py'
DEPLOY_SCRIPT = ROOT.parents[0] / 'deploy' / 'deploy_to_pi.sh'


def test_camera_module_is_canonical_and_deployed():
    assert WORK_CAMERA.is_file()

    deploy_script = DEPLOY_SCRIPT.read_text(encoding='utf-8')
    assert 'clinostat_extension/work/camera.py:$CLINOSTAT_ROOT/camera.py' in deploy_script
    assert (
        'cp -a $BACKUP_ROOT/clinostat/camera.py $CLINOSTAT_ROOT/camera.py'
        in deploy_script
    )


def test_extension_backups_are_required_before_transfer():
    deploy_script = DEPLOY_SCRIPT.read_text(encoding='utf-8')
    backup_section = deploy_script[
        deploy_script.index('say "백업: $BACKUP_ROOT"'):
        deploy_script.index('say "게이트웨이 전송"')
    ]

    assert '|| true' not in backup_section
    assert 'run_remote "test -f \'$remote\'" || die ' in backup_section
    assert (
        'run_remote "cp -a \'$remote\' '
        '\'$BACKUP_ROOT/clinostat/${remote#$CLINOSTAT_ROOT/}\'" || die '
        in backup_section
    )
