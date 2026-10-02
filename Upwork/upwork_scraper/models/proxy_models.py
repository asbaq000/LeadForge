from pydantic import BaseModel


class ProxyConfig(BaseModel):
    host: str
    port: int
    username: str
    password: str
    # Reported by the Webshare API; absent from the plain download list.
    country: str | None = None
    city: str | None = None

    @property
    def label(self) -> str:
        """Short human-readable id for logs — never includes the password."""
        where = ", ".join(filter(None, [self.city, self.country]))
        return f"{self.host}:{self.port}" + (f" ({where})" if where else "")

    def to_proxy_url(self) -> str:
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"

    def to_curl_cffi_dict(self) -> dict:
        url = self.to_proxy_url()
        return {"http": url, "https": url}
