import asyncio
import hashlib
import mimetypes
import time
from datetime import datetime
from pathlib import Path
from textwrap import shorten
from typing import Any

from curl_cffi import CurlFollow, CurlHttpVersion
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import HTTPError
from pydantic import BaseModel, ConfigDict

from gemini_webapi.constants import BROWSER_TYPE, Headers, format_http_version
from gemini_webapi.utils import logger


async def _fetch_bytes_resilient(
    url: str,
    req_client: AsyncSession | None = None,
    verbose: bool = False,
    request_timeout: float = 8.0,
) -> tuple[bytes | None, str | None]:
    """Fetches bytes through the authenticated Gemini session with a short deadline.

    Generated-media URLs frequently require the same cookies as the chat request.
    Keeping the retrieval on that session is both more reliable and prevents a
    failing CDN hop from turning one generation into several minutes of waits.
    """
    if not req_client:
        return None, None

    for headers in (None, Headers.REFERER.value):
        try:
            request = req_client.get(url, headers=headers) if headers else req_client.get(url)
            resp = await asyncio.wait_for(request, timeout=request_timeout)
            if resp.status_code == 200 and resp.content:
                return resp.content, resp.headers.get("content-type", "")
        except (asyncio.TimeoutError, Exception) as exc:
            if verbose:
                logger.debug(f"Authenticated image fetch failed for {url}: {exc}")

    return None, None


async def _download_image_chain(
    start_url: str,
    req_client: AsyncSession | None = None,
    max_hops: int = 6,
    verbose: bool = False,
    total_timeout: float = 20.0,
) -> tuple[bytes | None, str | None]:
    """Follows Google's text-URL hop chain (up to max_hops) to download binary image bytes."""
    curr_url = start_url
    deadline = time.monotonic() + total_timeout
    for hop in range(max_hops):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        content, ct = await _fetch_bytes_resilient(
            curr_url, req_client, verbose=verbose, request_timeout=min(8.0, remaining)
        )
        if not content or len(content) == 0:
            break
        # Check if content is binary image
        if (
            content.startswith(b"\x89PNG")
            or content.startswith(b"\xff\xd8\xff")
            or (content.startswith(b"RIFF") and b"WEBP" in content[:16])
            or (ct and "image" in ct)
        ):
            return content, ct
        # Check if content is a redirect URL text
        try:
            txt = content.decode("utf-8").strip()
            if txt.startswith("http://") or txt.startswith("https://"):
                curr_url = txt
                continue
        except Exception:
            pass
        # Neither image nor URL
        break
    return None, None


def _save_image_content(
    content: bytes,
    path_obj: Path,
    filename: str,
    content_type: str | None = None,
    verbose: bool = False,
) -> str:
    path_obj_file = Path(filename)
    if not path_obj_file.suffix:
        ext = None
        if content.startswith(b"\x89PNG"):
            ext = ".png"
        elif content.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif content.startswith(b"RIFF") and b"WEBP" in content[:16]:
            ext = ".webp"
        elif content_type:
            clean_ct = content_type.split(";")[0].strip().lower()
            ext = mimetypes.guess_extension(clean_ct)

        if not ext:
            ext = ".png"
        filename = f"{filename}{ext}"

    dest = path_obj / filename
    dest.write_bytes(content)
    if verbose:
        logger.info(f"Image saved as {dest.resolve()}")
    return str(dest.resolve())


class Image(BaseModel):
    """A single image object returned from Gemini.

    Parameters
    ----------
    url: `str`
        URL of the image.
    title: `str`, optional
        Title of the image, defaults to "[Image]".
    alt: `str`, optional
        Optional description of the image.
    proxy: `str`, optional
        Proxy used when saving image.
    client: `AsyncSession`, optional
        Used for saving file with authentication if needed.

    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    title: str = "[Image]"
    alt: str = ""
    proxy: str | None = None
    client: AsyncSession | None = None
    _default_filename_suffix: str = "image"

    def _get_url_for_hash(self) -> str:
        return self.url

    def __repr__(self) -> str:
        return (
            f"Image(title={self.title!r}, alt={shorten(self.alt, width=100)!r}, url={self.url!r})"
        )

    async def save(
        self,
        path: str = "temp",
        filename: str | None = None,
        verbose: bool = False,
        client: AsyncSession | None = None,
        **kwargs,
    ) -> str:
        """Saves the image to disk.

        Parameters
        ----------
        path: `str`, optional
            Directory path to save the image, defaults to "./temp".
        filename: `str | None`, optional
            File name to save the image. Defaults to a unique generated name.
        verbose: `bool`, optional
            If True, will print the path of the saved file or warning for invalid file name. Defaults to False.
        client: `AsyncSession | None`, optional
            Client used for requests.
        kwargs: `dict`, optional
            Additional arguments passed to the specific image's `_perform_save` implementation.
            For example, `GeneratedImage` accepts `full_size (bool)`.

        Returns
        -------
        `str`
            Absolute path of the saved image if successful.

        Raises
        ------
        `curl_cffi.requests.exceptions.HTTPError`
            If the network request failed.

        """
        if not filename or not Path(filename).suffix:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            url_hash = hashlib.sha256(self._get_url_for_hash().encode()).hexdigest()[:10]
            base_name = Path(filename).stem if filename else self._default_filename_suffix
            filename = f"{timestamp}_{url_hash}_{base_name}"

        close_client = False
        req_client = client or self.client
        if not req_client:
            client_ref = getattr(self, "client_ref", None)
            cookies = getattr(client_ref, "cookies", None) if client_ref else None
            impersonate: Any = (
                getattr(client_ref, "impersonate", BROWSER_TYPE) if client_ref else BROWSER_TYPE
            )
            req_client = AsyncSession(
                impersonate=impersonate,
                allow_redirects=CurlFollow.SAFE,
                http_version=CurlHttpVersion.NONE,
                cookies=cookies,
                proxy=self.proxy,
            )
            close_client = True

        try:
            path_obj = Path(path)
            path_obj.mkdir(parents=True, exist_ok=True)
            return await self._perform_save(req_client, path_obj, filename, verbose, **kwargs)
        finally:
            if close_client:
                await req_client.close()

    async def _perform_save(
        self, req_client: AsyncSession, path_obj: Path, filename: str, verbose: bool
    ) -> str:
        """Base implementation: multi-strategy resilient download."""
        authuser = getattr(getattr(self, "client_ref", None), "authuser", 0)
        auth_suffix = f"&authuser={authuser}" if authuser else ""

        urls_to_try = [
            self.url,
            f"{self.url}=d-I?alr=yes{auth_suffix}" if "=d-I" not in self.url else self.url,
        ]
        if "=s2048-rj" in self.url:
            urls_to_try.append(self.url.replace("=s2048-rj", "=s1024-rj"))
            urls_to_try.append(self.url.replace("=s2048-rj", "=s0"))
            urls_to_try.append(self.url.replace("=s2048-rj", ""))
        elif "=s1024-rj" in self.url:
            urls_to_try.append(self.url.replace("=s1024-rj", "=s2048-rj"))
            urls_to_try.append(self.url.replace("=s1024-rj", "=s0"))
            urls_to_try.append(self.url.replace("=s1024-rj", ""))
        else:
            urls_to_try.append(self.url + "=s2048-rj")
            urls_to_try.append(self.url + "=s1024-rj")
            urls_to_try.append(self.url + "=s0")

        for u in urls_to_try:
            content, ct = await _download_image_chain(u, req_client, verbose=verbose)
            if content and len(content) > 0:
                if (
                    content.startswith(b"\x89PNG")
                    or content.startswith(b"\xff\xd8\xff")
                    or (content.startswith(b"RIFF") and b"WEBP" in content[:16])
                    or (ct and "image" in ct)
                ):
                    return _save_image_content(content, path_obj, filename, ct, verbose)

        raise HTTPError(f"Error downloading image: all download strategies failed for {self.url}")


class WebImage(Image):
    """Image retrieved from web.

    Returned when asking Gemini to "SEND an image of [something]".
    """


class GeneratedImage(Image):
    """Image generated by Gemini.

    Returned when asking Gemini to "GENERATE an image of [something]".

    Parameters
    ----------
    client_ref: `GeminiClient`, optional
        Reference to the GeminiClient instance.
    cid: `str`, optional
        Chat ID.
    rid: `str`, optional
        Reply ID.
    rcid: `str`, optional
        Reply candidate ID.
    image_id: `str`, optional
        Image ID generated.

    """

    client_ref: Any = None
    cid: str = ""
    rid: str = ""
    rcid: str = ""
    image_id: str = ""

    # @override
    async def _perform_save(
        self,
        req_client: AsyncSession,
        path_obj: Path,
        filename: str,
        verbose: bool,
        full_size: bool = True,
    ) -> str:
        """Internal method for saving GeneratedImage, handling full size resolution.

        Parameters
        ----------
        req_client: `AsyncSession`
             Client used for requests.
        path_obj: `Path`
            Path to save the image.
        filename: `str`
            Base filename.
        verbose: `bool`
            Prints status if True.
        full_size: `bool`, optional
            Modifies preview URLs to fetch full-size images. Defaults to True.

        Returns
        -------
        `str`
            Absolute path of the saved image if successfully saved.

        """
        if full_size:
            authuser = getattr(self.client_ref, "authuser", 0) if self.client_ref else 0
            auth_suffix = f"&authuser={authuser}" if authuser else ""
            if all([self.client_ref, self.cid, self.rid, self.rcid, self.image_id]):
                try:
                    original_url = await self.client_ref._get_full_size_image(
                        cid=self.cid,
                        rid=self.rid,
                        rcid=self.rcid,
                        image_id=self.image_id,
                    )
                    if original_url:
                        req_url = f"{original_url}=d-I?alr=yes{auth_suffix}"
                        content, ct = await _download_image_chain(req_url, req_client, verbose=verbose)
                        if content and len(content) > 0 and (
                            content.startswith(b"\x89PNG")
                            or content.startswith(b"\xff\xd8\xff")
                            or (content.startswith(b"RIFF") and b"WEBP" in content[:16])
                            or (ct and "image" in ct)
                        ):
                            return _save_image_content(content, path_obj, filename, ct, verbose)
                except Exception as e:
                    logger.debug(
                        f"Failed to fetch full size image URL via RPC: {e}, falling back to default URL suffix."
                    )

            if self.url:
                try:
                    req_url = f"{self.url}=d-I?alr=yes{auth_suffix}" if "=d-I" not in self.url else self.url
                    content, ct = await _download_image_chain(req_url, req_client, verbose=verbose)
                    if content and len(content) > 0 and (
                        content.startswith(b"\x89PNG")
                        or content.startswith(b"\xff\xd8\xff")
                        or (content.startswith(b"RIFF") and b"WEBP" in content[:16])
                        or (ct and "image" in ct)
                    ):
                        return _save_image_content(content, path_obj, filename, ct, verbose)
                except Exception:
                    pass

            if "=s1024-rj" in self.url:
                self.url = self.url.replace("=s1024-rj", "=s2048-rj")
            elif "=s2048-rj" not in self.url:
                self.url += "=s2048-rj"
        elif "=s2048-rj" in self.url:
            self.url = self.url.replace("=s2048-rj", "=s1024-rj")
        elif "=s1024-rj" not in self.url:
            self.url += "=s1024-rj"

        return await super()._perform_save(req_client, path_obj, filename, verbose)
