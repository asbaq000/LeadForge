import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests

from upwork_scraper.config import PAGE_SIZE
from upwork_scraper.errors import TokenExpired
from upwork_scraper.models.job_models import Job, JobList
from upwork_scraper.proxies.proxy_manager import ProxyManager, proxy_dict_for

LOGGER = logging.getLogger(__name__)

UPWORK_GRAPHQL_URL = "https://www.upwork.com/api/graphql/v1"
MAX_OFFSET = 5000  # Upwork API caps pagination at offset ~5000
CONCURRENT_WORKERS = 10

GRAPHQL_QUERY = """
query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {
  search {
    universalSearchNuxt {
      visitorJobSearchV1(request: $requestVariables) {
        paging {
          total
          offset
          count
        }
        results {
          id
          title
          description
          ontologySkills {
            prefLabel
          }
          jobTile {
            job {
              id
              ciphertext: cipherText
              jobType
              hourlyBudgetMax
              hourlyBudgetMin
              contractorTier
              publishTime
              hourlyEngagementDuration {
                weeks
              }
              fixedPriceAmount {
                amount
              }
              fixedPriceEngagementDuration {
                weeks
              }
            }
          }
        }
      }
    }
  }
}
"""

HEADERS_BASE = {
    "Accept": "*/*",
    "Accept-Language": "en-GB,en;q=0.7,en-US;q=0.3",
    "Accept-Encoding": "gzip",
    "Referer": "https://www.upwork.com/nx/search/jobs/?",
    "X-Upwork-Accept-Language": "en-US",
    "Content-Type": "application/json",
}


def fetch_page(
    token: str,
    proxy_dict: dict | None,
    offset: int = 0,
    count: int = PAGE_SIZE,
    query: str | None = None,
    sort: str = "recency",
) -> JobList:
    """Fetch one page and return it with the search's overall result count."""
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {token}"}
    request_variables = {
        "sort": sort,
        # Highlighting wraps every query match in literal `H^ ... ^H` markers,
        # so keep it off whenever a query is in play. With no query there is
        # nothing to highlight and the responses are identical either way.
        "highlight": not query,
        "paging": {"offset": offset, "count": count},
    }
    if query:
        request_variables["userQuery"] = query

    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {"requestVariables": request_variables},
    }

    resp = requests.post(
        UPWORK_GRAPHQL_URL,
        headers=headers,
        json=payload,
        proxies=proxy_dict,
        impersonate="chrome",
        timeout=20,
    )

    if resp.status_code == 401:
        raise TokenExpired("Upwork returned 401 — token expired")

    resp.raise_for_status()
    return JobList.model_validate(resp.json())


def fetch_jobs_page(
    token: str,
    proxy_dict: dict | None,
    offset: int = 0,
    count: int = PAGE_SIZE,
    query: str | None = None,
    sort: str = "recency",
) -> list[Job]:
    """Fetch a single page of jobs. `query` filters by keyword when given."""
    return fetch_page(token, proxy_dict, offset, count, query, sort).jobs


def fetch_all_jobs(
    token: str,
    proxy_manager: ProxyManager,
    max_pages: int = 100,
    query: str | None = None,
    sort: str = "recency",
    workers: int = CONCURRENT_WORKERS,
    pinned_proxy_dict: dict | None = None,
) -> list[Job]:
    """Fetch up to `max_pages` pages concurrently and return every job found.

    The first page is fetched on its own to learn how many results the search
    actually has, so a keyword with 200 matches costs 4 requests instead of
    `max_pages`. That matters most when backfilling many keywords at once.

    By default each page picks its own proxy from `proxy_manager`. Pass
    `pinned_proxy_dict` to send every page through one fixed proxy — useful
    because the visitor token is issued against the egress IP that requested it.
    """
    def _proxy() -> dict | None:
        return pinned_proxy_dict or proxy_dict_for(proxy_manager.get_proxy())

    first = fetch_page(token, _proxy(), offset=0, query=query, sort=sort)
    all_jobs: list[Job] = list(first.jobs)

    # Cap requested pages by the API's pagination limit and by how many results
    # the search reports. An unknown total falls back to the requested depth.
    reachable = MAX_OFFSET + PAGE_SIZE
    if first.total is not None:
        reachable = min(reachable, first.total)
    max_offset = min(max_pages * PAGE_SIZE, reachable)
    offsets = list(range(PAGE_SIZE, max_offset, PAGE_SIZE))

    if not offsets:
        LOGGER.info(
            "Fetched %d jobs from 1 page (total available: %s)",
            len(all_jobs), first.total,
        )
        return all_jobs

    failed = 0

    def _fetch_page(offset: int) -> tuple[int, list[Job]]:
        jobs = fetch_jobs_page(token, _proxy(), offset=offset, query=query, sort=sort)
        return offset, jobs

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_fetch_page, o): o for o in offsets}

        token_expired = None
        for future in as_completed(futures):
            offset = futures[future]
            try:
                _, jobs = future.result()
                all_jobs.extend(jobs)
            except TokenExpired as e:
                token_expired = e
                for f in futures:
                    f.cancel()
                break
            except Exception:
                failed += 1
                LOGGER.warning("Failed to fetch offset %d", offset, exc_info=True)

    if token_expired:
        raise token_expired

    LOGGER.info(
        "Fetched %d jobs from %d pages (%d failed, total available: %s)",
        len(all_jobs), len(offsets) + 1 - failed, failed, first.total,
    )
    return all_jobs
