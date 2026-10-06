from httpx import AsyncClient

PASSWORD = "correct-horse-battery"


async def csrf(client: AsyncClient) -> dict[str, str]:
    """Any GET hands out the CSRF cookie; state-changing requests must echo it in a header."""
    if "csrftoken" not in client.cookies:
        await client.get("/api/health")
    return {"X-CSRFToken": client.cookies["csrftoken"]}


async def register(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/api/auth/register", json={"email": email, "password": password}, headers=await csrf(client)
    )


async def login(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post(
        "/api/auth/login", data={"username": email, "password": password}, headers=await csrf(client)
    )


async def sign_up_and_login(client: AsyncClient, email: str) -> None:
    assert (await register(client, email)).status_code == 201
    assert (await login(client, email)).status_code == 204
