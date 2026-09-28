"""
3D 가상 건물 시연 페이지(/demo3d).

프론트엔드를 같은 주소에서 서빙하면 모든 경로를 잡는 catch-all 이 붙는다.
/demo3d 가 그보다 뒤에 등록되거나 슬래시 처리가 빠지면 3D 대신 대시보드가 뜬다.
"""
from fastapi.testclient import TestClient

from app.main import app


def test_demo3d_redirects_to_trailing_slash():
    # 슬래시가 없으면 페이지 안의 ./demo3d.js 같은 상대 경로가 엉뚱한 곳을 가리킨다
    with TestClient(app) as client:
        resp = client.get("/demo3d", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"].endswith("/demo3d/")


def test_demo3d_page_and_assets_are_served():
    with TestClient(app) as client:
        page = client.get("/demo3d/")
        assert page.status_code == 200
        assert "3D 가상 건물" in page.text

        # three.js 를 서버에 함께 넣어 두었다. 대회장에서 CDN 이 막혀도 떠야 한다.
        for path in (
            "/demo3d/demo3d.js",
            "/demo3d/vendor/three.module.js",
            "/demo3d/vendor/three.core.js",
            "/demo3d/vendor/OrbitControls.js",
            "/demo3d/vendor/CSS2DRenderer.js",
        ):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert "javascript" in resp.headers["content-type"], path
