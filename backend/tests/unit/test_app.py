from fastapi import status


def test_openapi_schema_is_available(client):
    response = client.get("/openapi.json")

    assert response.status_code == status.HTTP_200_OK
