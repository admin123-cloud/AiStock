from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_uses_react_context_and_release_dockerfile():
    script = (ROOT / 'scripts' / 'release_app.ps1').read_text(encoding='utf-8')
    compose = (ROOT / 'docker-compose.release.yml').read_text(encoding='utf-8')
    assert "frontend-react\\Dockerfile.release" in script
    assert "(Join-Path $taskRepo 'frontend-react')" in script
    assert "aistock-core-frontend:${AISTOCK_RELEASE_VERSION" in compose
    assert 'AISTOCK_RELEASE_VERSION' in compose


def test_react_release_image_has_spa_fallback_and_api_proxy():
    dockerfile = ROOT / 'frontend-react' / 'Dockerfile.release'
    nginx = ROOT / 'frontend-react' / 'nginx.release.conf'
    assert dockerfile.exists() and 'npm run build' in dockerfile.read_text(encoding='utf-8')
    config = nginx.read_text(encoding='utf-8')
    assert 'proxy_pass http://backend:8000' in config
    assert 'try_files $uri $uri/ /index.html' in config
