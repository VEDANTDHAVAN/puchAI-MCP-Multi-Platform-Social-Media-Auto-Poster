# type: ignore
import asyncio
from typing import Annotated
import os
import requests
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent, ImageContent, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import BaseModel, Field, AnyUrl

import markdownify
import httpx
import readabilipy

# --- Load environment variables ---
load_dotenv()

TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")

assert TOKEN is not None, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER is not None, "Please set MY_NUMBER in your .env file"

# --- Auth Provider ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(
                token=token,
                client_id="puch-client",
                scopes=["*"],
                expires_at=None,
            )
        return None

# --- Rich Tool Description model ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None = None

# --- Fetch Utility Class ---
class Fetch:
    USER_AGENT = "Puch/1.0 (Autonomous)"

    @classmethod
    async def fetch_url(
        cls,
        url: str,
        user_agent: str,
        force_raw: bool = False,
    ) -> tuple[str, str]:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    url,
                    follow_redirects=True,
                    headers={"User-Agent": user_agent},
                    timeout=30,
                )
            except httpx.HTTPError as e:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url}: {e!r}"))

            if response.status_code >= 400:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to fetch {url} - status code {response.status_code}"))

            page_raw = response.text

        content_type = response.headers.get("content-type", "")
        is_page_html = "text/html" in content_type

        if is_page_html and not force_raw:
            return cls.extract_content_from_html(page_raw), ""

        return (
            page_raw,
            f"Content type {content_type} cannot be simplified to markdown, but here is the raw content:\n",
        )

    @staticmethod
    def extract_content_from_html(html: str) -> str:
        """Extract and convert HTML content to Markdown format."""
        ret = readabilipy.simple_json.simple_json_from_html_string(html, use_readability=True)
        if not ret or not ret.get("content"):
            return "<error>Page failed to be simplified from HTML</error>"
        content = markdownify.markdownify(ret["content"], heading_style=markdownify.ATX)
        return content

    @staticmethod
    async def google_search_links(query: str, num_results: int = 5) -> list[str]:
        """
        Perform a scoped DuckDuckGo search and return a list of job posting URLs.
        (Using DuckDuckGo because Google blocks most programmatic scraping.)
        """
        ddg_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        links = []

        async with httpx.AsyncClient() as client:
            resp = await client.get(ddg_url, headers={"User-Agent": Fetch.USER_AGENT})
            if resp.status_code != 200:
                return ["<error>Failed to perform search.</error>"]

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", class_="result__a", href=True):
            href = a["href"]
            if "http" in href:
                links.append(href)
            if len(links) >= num_results:
                break

        return links or ["<error>No results found.</error>"]

# --- MCP Server Setup ---
mcp = FastMCP(
    "Multi-Platform Social Media Auto-Poster MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

# --- Tool: social_media_poster (now smart!) ---
# LinkedIn post creation and publishing
# Tool Description
LINKEDIN_POST_DESCRIPTION = RichToolDescription(
    description="Create a LinkedIn post with optional images.",
    use_when="Use this when the user wants to post an update on LinkedIn, either text-only or with an image.",
    side_effects="Publishes a post to the authenticated LinkedIn account, optionally including uploaded images."
)

@mcp.tool(description=LINKEDIN_POST_DESCRIPTION.model_dump_json())
async def post_to_linkedin(post_text: str) -> str:
    """
    Create a text-only post on LinkedIn using the authenticated user's account.
    """
    import os
    access_token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    if not access_token:
        return {"error": "Missing LinkedIn access token. Set LINKEDIN_ACCESS_TOKEN env variable."}
        
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    # Step 1: Get user URN
    profile_res = requests.get(
        "https://api.linkedin.com/v2/me",
        headers=headers
    )
        
    if profile_res.status_code != 200:
        return f"Failed to fetch profile: {profile_res.text}"
    
    user_urn = profile_res.json().get("id")
    author_urn = f"urn:li:person:{user_urn}"
    if not user_urn:
        return {"error": "Could not retrieve LinkedIn user URN"}
        
    # Step 2: Text-only post
    post_payload = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {
                    "text": post_text
                },
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }

    # Step 3: Post to LinkedIn
    post_res = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers=headers,
        json=post_payload
    )
        
    if post_res.status_code != 201:
        return {"error": f"Failed to post on LinkedIn: {post_res.text}"}

    return [TextContent(type="text", text="✅ LinkedIn post published successfully!")]
     
# --- Run MCP Server ---
async def main():
    print("🚀 Starting MCP server on http://0.0.0.0:8085")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8085)

if __name__ == "__main__":
    asyncio.run(main())
