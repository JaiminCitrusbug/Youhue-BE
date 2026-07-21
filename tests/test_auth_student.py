"""Student passwordless sign-in + single-active-device (INFRA-01 / FR-01-08)."""
SIGNIN = "/api/v1/auth/student/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_student_signin_success(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    r = client.post(SIGNIN, json={"school_code": "OAK-77", "student_id": str(student.id)})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_wrong_school_code_rejected(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    r = client.post(SIGNIN, json={"school_code": "WRONG", "student_id": str(student.id)})
    assert r.status_code == 401


def test_single_active_device(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    t1 = client.post(SIGNIN, json={"school_code": "OAK-77", "student_id": str(student.id), "device_id": "dev1"}).json()["access_token"]
    assert client.get("/api/v1/me", headers=_auth(t1)).status_code == 200
    # a new-device sign-in ends the first session
    t2 = client.post(SIGNIN, json={"school_code": "OAK-77", "student_id": str(student.id), "device_id": "dev2"}).json()["access_token"]
    assert client.get("/api/v1/me", headers=_auth(t1)).status_code == 401
    assert client.get("/api/v1/me", headers=_auth(t2)).status_code == 200
