from pathlib import Path
import subprocess,sys
root=Path.cwd()
a=(root/'scripts/ops/activate_vps_release.sh').read_text()
d=(root/'scripts/ops/deploy_vps.sh').read_text()
n=(root/'deploy/cloud/nginx.conf.template').read_text()
mode=sys.argv[1]
if mode=='control':
    subprocess.run(['bash','-n',str(root/'scripts/ops/activate_vps_release.sh')],check=True)
    print('activation_syntax=valid')
elif mode=='exploit':
    rollback=a.split('rollback() {',1)[1].split('trap cleanup EXIT',1)[0]
    print('PPV_REACHED',flush=True)
    assert 'release_database.py" restore' in rollback, 'rollback must restore database before old pointer'
    assert rollback.index('release_database.py" restore') < rollback.index('mv -Tf')
elif mode=='variant':
    print('PPV_REACHED',flush=True)
    assert 'release_database.py" snapshot' in a, 'checkpoint must be restore-proven before migration'
    assert a.index('release_database.py" snapshot') < a.index('switched=1')
elif mode=='behavior':
    assert r'^sro-v[0-9]+\.[0-9]+\.[0-9]+$' in d
    assert 'proxy_set_header X-Forwarded-Proto https;' in n
    print('release_tag=explicit https_forwarding=preserved')
elif mode=='security':
    assert 'IdentitiesOnly=yes' in d and 'chmod 0600' in d
    assert 'ssl_certificate_key /etc/letsencrypt/' in n
    print('dedicated_ssh_identity=preserved private_environment=preserved tls_key_reference=preserved')
elif mode=='regression':
    subprocess.run(['bash','-n',str(root/'scripts/ops/deploy_vps.sh')],check=True)
    print('deployment_syntax=valid')
