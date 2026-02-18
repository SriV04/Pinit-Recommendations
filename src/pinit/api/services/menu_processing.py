"""
Restaurant Menu Crawling & Dietary Analysis Service
=====================================================
Discovers menu pages on restaurant websites, extracts content
(HTML, PDF, images), and uses xAI Grok to analyse dietary friendliness.

Ported from login/ai/menus/main.py for use within the Pinit-Recommendations API.

Usage:
    await process_menu_for_location(
        location_id=123,
        google_place_id="ChIJ...",
        website="https://example-restaurant.com",
        restaurant_name="Example Restaurant",
        cuisine_hint="Italian",
    )
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
import pdfplumber
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from openai import AsyncOpenAI

from pinit.config.secrets import XAI_API_KEY
from pinit.integrations.supabase import get_supabase_service

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

XAI_BASE_URL = "https://api.x.ai/v1"
MODEL = "grok-4-fast-non-reasoning"

MENU_PATH_PATTERNS = [
    "/menu", "/food", "/our-menu", "/food-menu", "/food-drink",
    "/food-and-drink", "/eat", "/dine", "/dishes", "/carte",
    "/a-la-carte", "/lunch", "/dinner", "/brunch",
    "/menus", "/the-menu",
]

MENU_PATH_ANTI_PATTERNS = [
    "drinks-menu", "drinks", "wine-list", "cocktail-menu",
    "/about", "/contact", "/locations", "/privacy", "/terms",
    "/blog", "/news", "/events", "/careers", "/jobs",
]

MENU_LINK_KEYWORDS = [
    "menu", "food", "dishes", "carte", "dine", "eat",
    "lunch", "dinner", "brunch", "drinks", "kitchen",
]

DIETARY_REQUIREMENTS = [
    "vegetarian",
    "vegan",
    "gluten-free",
    "dairy-free",
    "nut-free",
    "pescatarian",
]

MAX_PAGES_PER_SITE = 15
MAX_PDF_DOWNLOADS = 3
MAX_IMAGE_DOWNLOADS = 3

# ── PDF scoring keywords ────────────────────────────────────────────────────

PDF_MENU_KEYWORDS = [
    "menu", "food", "alc", "a-la-carte", "carte", "lunch", "dinner",
    "brunch", "drinks", "beverage", "bev", "feast", "tasting",
    "supper", "breakfast", "dish",
]

PDF_NON_MENU_KEYWORDS = [
    "nutritional", "allergen", "allergy", "wine-list", "cocktail",
    "privacy", "terms", "policy", "report", "press",
]

# ── Image scoring keywords ──────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

IMAGE_MENU_KEYWORDS = [
    "menu", "food", "carte", "alc", "a-la-carte", "dinner", "lunch",
    "brunch", "breakfast", "dish", "feast", "tasting", "supper",
]

IMAGE_NON_MENU_KEYWORDS = [
    "logo", "icon", "decor", "bg", "background", "arrow", "close",
    "banner", "hero", "header", "footer", "social", "avatar", "profile",
    "button", "graphic", "pattern", "texture", "quarter-arch", "key",
]


# ── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class DietaryScore:
    requirement: str
    dish_count: int
    friendliness_pct: int  # 0, 25, 60, 80

    @staticmethod
    def compute_pct(count: int) -> int:
        if count == 0:
            return 0
        elif count <= 2:
            return 25
        elif count <= 5:
            return 60
        else:
            return 80


@dataclass
class MenuAnalysisResult:
    google_place_id: str
    website: str
    menu_url: Optional[str] = None
    menu_found: bool = False
    menu_markdown: Optional[str] = None
    restaurant_description: Optional[str] = None
    total_dishes: int = 0
    dietary_scores: List[DietaryScore] = field(default_factory=list)
    halal_mentioned: bool = False
    confidence: str = "low"
    reccomended_dishes: Optional[str] = None
    detected_cuisine: Optional[str] = None
    cuisine_scores: Optional[Dict[str, float]] = None
    notes: str = ""
    error: Optional[str] = None


# ── PDF Detection & Extraction ───────────────────────────────────────────────

def _score_pdf_as_menu(url: str, link_text: str = "") -> int:
    """Score how likely a PDF link is to be a food menu."""
    score = 0
    path = urlparse(url).path.lower()
    filename = path.split("/")[-1].lower()
    text = link_text.lower()

    for keyword in PDF_MENU_KEYWORDS:
        if keyword in filename:
            score += 50
            break
    for keyword in PDF_MENU_KEYWORDS:
        if keyword in text:
            score += 40
            break
    for keyword in PDF_NON_MENU_KEYWORDS:
        if keyword in filename or keyword in text:
            score -= 60
            break
    if "download" in text:
        score += 20
    season_words = ["winter", "spring", "summer", "autumn", "jan", "feb", "mar", "apr", "may", "jun"]
    if any(s in filename for s in season_words):
        score += 10

    return score


def extract_pdf_links(markdown: str, base_url: str) -> List[Tuple[int, str]]:
    """Extract PDF links from crawled markdown content, sorted by score descending."""
    link_pattern = r'\[([^\]]*)\]\((https?://[^\s\)]+\.pdf[^\s\)]*)\)'
    bare_pattern = r'(https?://[^\s\)]+\.pdf[^\s\)]*)'

    candidates: Dict[str, Tuple[int, str]] = {}

    for match in re.finditer(link_pattern, markdown, re.IGNORECASE):
        text, url = match.group(1), match.group(2)
        full_url = urljoin(base_url, url)
        score = _score_pdf_as_menu(full_url, text)
        if full_url not in candidates or score > candidates[full_url][0]:
            candidates[full_url] = (score, full_url)

    for match in re.finditer(bare_pattern, markdown, re.IGNORECASE):
        url = match.group(1)
        full_url = urljoin(base_url, url)
        if full_url not in candidates:
            score = _score_pdf_as_menu(full_url, "")
            candidates[full_url] = (score, full_url)

    return sorted(candidates.values(), key=lambda x: -x[0])


async def download_and_extract_pdf(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Download a PDF and extract its text content using pdfplumber."""
    try:
        response = await client.get(url, follow_redirects=True, timeout=15.0)
        if response.status_code != 200:
            return None

        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            return None

        pdf_bytes = response.content
        if len(pdf_bytes) < 100:
            return None

        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

        if not text_parts:
            return None

        return "\n\n".join(text_parts)
    except Exception as e:
        logger.warning("PDF extraction error for %s: %s", url, e)
        return None


async def extract_pdfs_from_markdown(
    markdown: str,
    base_url: str,
    max_pdfs: int = MAX_PDF_DOWNLOADS,
) -> List[Tuple[str, str]]:
    """Find PDF links in markdown, download and extract text from top candidates."""
    pdf_candidates = extract_pdf_links(markdown, base_url)
    if not pdf_candidates:
        return []

    top_candidates = [(score, url) for score, url in pdf_candidates if score > -10][:max_pdfs]
    if not top_candidates:
        return []

    results = []
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; PinItBot/1.0)"},
    ) as http_client:
        for _score, url in top_candidates:
            text = await download_and_extract_pdf(url, http_client)
            if text:
                results.append((url, text))

    return results


# ── Image Detection & Extraction ─────────────────────────────────────────────

def _score_image_as_menu(url: str) -> int:
    """Score how likely an image URL is to be a menu photo."""
    score = 0
    path = urlparse(url).path.lower()
    filename = path.split("/")[-1].lower()

    if not any(filename.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return -100

    for keyword in IMAGE_MENU_KEYWORDS:
        if keyword in filename:
            score += 50
            break
    for keyword in IMAGE_NON_MENU_KEYWORDS:
        if keyword in filename:
            score -= 80
            return score

    if "scaled" in filename or "2000x" in filename or "1440x" in filename:
        score += 15
    if "/uploads/" in path:
        score += 20
    if "/themes/" in path or "/library/" in path or "/assets/" in path:
        score -= 40
    if "800x800" in path or "150x" in path or "100x" in path:
        score -= 10
    if "2000x" in path or "1440x" in path or "scaled" in path:
        score += 10

    return score


def extract_image_links(markdown: str, base_url: str) -> List[Tuple[int, str]]:
    """Extract image links from crawled markdown content, sorted by score descending."""
    image_pattern = r'!\[[^\]]*\]\(([^\s\)]+)\)'
    candidates: Dict[str, Tuple[int, str]] = {}

    for match in re.finditer(image_pattern, markdown):
        url = match.group(1)
        full_url = urljoin(base_url, url)
        score = _score_image_as_menu(full_url)
        if full_url not in candidates or score > candidates[full_url][0]:
            candidates[full_url] = (score, full_url)

    return sorted(candidates.values(), key=lambda x: -x[0])


async def download_menu_images(
    markdown: str,
    base_url: str,
    max_images: int = MAX_IMAGE_DOWNLOADS,
) -> List[Tuple[str, str, str]]:
    """
    Find likely menu images in markdown, download them.
    Returns list of (url, base64_data, media_type).
    """
    image_candidates = extract_image_links(markdown, base_url)
    if not image_candidates:
        return []

    top_candidates = [(score, url) for score, url in image_candidates if score > 0][:max_images]
    if not top_candidates:
        return []

    results = []
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; PinItBot/1.0)"},
    ) as http_client:
        for _score, url in top_candidates:
            try:
                response = await http_client.get(url, follow_redirects=True, timeout=15.0)
                if response.status_code != 200:
                    continue

                content_type = response.headers.get("content-type", "")
                if "png" in content_type or url.lower().endswith(".png"):
                    media_type = "image/png"
                elif "webp" in content_type or url.lower().endswith(".webp"):
                    media_type = "image/webp"
                else:
                    media_type = "image/jpeg"

                img_bytes = response.content
                if len(img_bytes) < 1000:
                    continue

                b64_data = base64.b64encode(img_bytes).decode("utf-8")
                results.append((url, b64_data, media_type))
            except Exception as e:
                logger.warning("Image download error for %s: %s", url, e)
                continue

    return results


# ── Menu URL Discovery ───────────────────────────────────────────────────────

def _score_url_as_menu(url: str, link_text: str = "") -> int:
    """Heuristic score for how likely a URL is a menu page."""
    score = 0
    path = urlparse(url).path.lower().rstrip("/")
    text = link_text.lower()

    if path in MENU_PATH_PATTERNS:
        score += 100
    for pattern in MENU_PATH_PATTERNS:
        if pattern.strip("/") in path:
            score += 50
            break
    for keyword in MENU_LINK_KEYWORDS:
        if keyword in text:
            score += 30
            break
    for keyword in MENU_PATH_ANTI_PATTERNS:
        if keyword in path or keyword in text:
            score -= 50
            break
    if path.endswith(".pdf"):
        score -= 20
    if path.count("/") > 3:
        score -= 10

    return score


def _contains_menu_content(markdown: str) -> bool:
    """Detect if markdown contains actual menu content (dishes, prices, etc.)."""
    if not markdown or len(markdown) < 300:
        return False

    price_patterns = [
        r'£\d+\.\d{2}', r'\$\d+\.\d{2}', r'€\d+\.\d{2}',
        r'£\d+', r'\$\d+',
    ]
    price_count = 0
    for pattern in price_patterns:
        price_count += len(re.findall(pattern, markdown))

    dish_keywords = [
        'starters', 'mains', 'desserts', 'appetizers', 'entrees',
        'served with', 'topped with', 'garnished', 'grilled', 'fried',
        'roasted', 'baked', 'steamed',
    ]
    keyword_count = sum(1 for keyword in dish_keywords if keyword.lower() in markdown.lower())

    return price_count >= 5 and keyword_count >= 3


async def discover_menu_url(
    base_url: str, crawler: AsyncWebCrawler
) -> Tuple[Optional[str], str]:
    """Crawl the homepage and discover the most likely menu page URL."""
    config = CrawlerRunConfig(
        word_count_threshold=10,
        exclude_external_links=True,
        excluded_tags=["footer", "script", "style", "noscript", "iframe"],
        process_iframes=False,
        page_timeout=60000,
        delay_before_return_html=2.0,
    )

    result = await crawler.arun(url=base_url, config=config)
    if not result.success:
        logger.warning("Failed to crawl %s: %s", base_url, result.error_message)
        return None, ""

    homepage_md = result.markdown or ""

    # Strategy 0: Check if homepage itself contains menu
    if _contains_menu_content(homepage_md):
        logger.info("Homepage itself contains menu content for %s", base_url)
        return base_url, homepage_md

    # Strategy 1: Check internal links found on the page
    candidates = []
    if result.links and "internal" in result.links:
        for link in result.links["internal"]:
            href = link.get("href", "")
            text = link.get("text", "")
            if not href:
                continue
            full_url = urljoin(base_url, href)
            score = _score_url_as_menu(full_url, text)
            if score > 0:
                candidates.append((score, full_url))

    # Strategy 2: Try common paths directly
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for pattern in MENU_PATH_PATTERNS[:8]:
        pattern_url = urljoin(base, pattern)
        candidates.append((40, pattern_url))

    if not candidates:
        return None, homepage_md

    # Deduplicate and sort by score
    seen: set = set()
    unique = []
    for score, url in sorted(candidates, key=lambda x: -x[0]):
        normalised = url.rstrip("/").lower()
        if normalised not in seen:
            seen.add(normalised)
            unique.append((score, url))

    # Try the top candidates until one works
    for candidate_url_score in unique[:5]:
        _score, candidate_url = candidate_url_score
        try:
            menu_result = await crawler.arun(url=candidate_url, config=config)
            if menu_result.success and menu_result.markdown and len(menu_result.markdown) > 200:
                logger.info("Found menu at: %s", candidate_url)
                return candidate_url, homepage_md
        except Exception:
            continue

    return None, homepage_md


# ── Menu Content Extraction ──────────────────────────────────────────────────

async def extract_menu_content(
    menu_url: str,
    crawler: AsyncWebCrawler,
) -> Tuple[Optional[str], List[Tuple[str, str, str]]]:
    """
    Fetch the menu page and return cleaned markdown + any menu images.
    Returns (text_content, menu_images).
    """
    config = CrawlerRunConfig(
        word_count_threshold=2,
        process_iframes=False,
        page_timeout=60000,
        delay_before_return_html=2.0,
    )

    result = await crawler.arun(url=menu_url, config=config)
    if not result.success or not result.markdown:
        return None, []

    markdown = result.markdown

    # Basic cleanup
    lines = markdown.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if len(stripped) < 3:
            continue
        if any(skip in stripped.lower() for skip in [
            "cookie", "privacy policy", "terms of service",
            "all rights reserved", "©", "instagram", "facebook",
            "sign up for", "subscribe to", "newsletter",
        ]):
            continue
        cleaned.append(line)

    content = "\n".join(cleaned).strip()

    # Check for PDFs
    pdf_results = await extract_pdfs_from_markdown(markdown, menu_url)
    if pdf_results:
        pdf_sections = []
        for _idx, (pdf_url, pdf_text) in enumerate(pdf_results):
            filename = pdf_url.split("/")[-1]
            truncated = pdf_text[:5000]
            pdf_sections.append(f"### Menu PDF: {filename}\n{truncated}")
        content = content + "\n\n## Extracted from PDF Menus\n" + "\n\n".join(pdf_sections)

    # Check for menu images
    menu_images = await download_menu_images(markdown, menu_url)

    # Cap total content
    if len(content) > 15000:
        content = content[:15000] + "\n\n[... menu truncated ...]"

    return content, menu_images


# ── LLM Analysis ─────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are analysing a restaurant's website content to extract menu information and assess dietary friendliness.

## Input
**Restaurant name:** {restaurant_name}
**Known cuisine type:** {cuisine_hint}

**Restaurant website content (may include homepage + menu page):**
{content}

## Your Tasks

### 1. Restaurant Description (MANDATORY — you must ALWAYS produce this)
Write a 2-3 sentence description of this restaurant. Cover the cuisine type, vibe/atmosphere, price point, and what makes it distinctive. Write it as if for a food discovery app — enticing but factual.
Use ALL available signals: the homepage text, imagery descriptions, restaurant name, the cuisine hint provided, and any other clues on the page (about us sections, taglines, location info, social media bios, review snippets, etc.).
IMPORTANT: The menu page being unavailable does NOT mean you cannot describe the restaurant. A broken menu page is irrelevant to this task. You have the homepage, the restaurant name, and the cuisine type — that is always enough. Never return "Unable to provide description" or similar. Always write something useful.

### 2. Menu Extraction & Dietary Analysis
If a menu is available, go through every dish. For each dietary requirement below, count how many distinct dishes a person with ONLY that requirement could eat.

If NO menu is available (e.g. 404, site under construction, content is just navigation):
- Set total_dishes to 0
- Set all dietary counts to 0
- Set confidence to "no_menu"
- Still provide the description above

Be practical when counting:
- A salad with cheese is vegetarian but NOT vegan or dairy-free
- Plain grilled fish is pescatarian, gluten-free, dairy-free, nut-free, and keto-friendly
- If a dish COULD be modified (e.g. "ask for no cheese"), only count it if the menu explicitly offers that option
- For halal: Search the ENTIRE page content for ANY mention of "halal" (certification, labels, descriptions, about us, footer, anywhere). If halal is mentioned anywhere, set halal_mentioned to true. If not mentioned at all, set halal_mentioned to false. Do NOT try to count halal dishes — just report whether it's mentioned.
- For kosher: unless the restaurant explicitly states kosher certification or specific dishes are marked, count as 0 and note this
- You should be strict, if there is some doubt as to whether someone with a specific requirement could have it, do not assume they can
- If the menu is too vague to determine ingredients, mark confidence as "low"

### 3. Key dish extraction

If the menu exists, extract 2 dishes from the menu that we would be recommending to users. The criteria for these is:

The first one should be a staple that you have high confidence that would be good
The second one should be something unique to that restaurant, something unusual that may be interesting to try

Dietary requirements:
{dietary_list}

### 3. Output Format
Return ONLY valid JSON, no markdown fences:
{{
  "menu_url": "<URL of the menu page, or null if no menu found>",
  "description": "<2-3 sentence restaurant description — ALWAYS provide this>",
  "total_dishes": <total number of distinct dishes on the menu, 0 if no menu>,
  "dietary_counts": {{
    "vegetarian": <int>,
    "vegan": <int>,
    "gluten-free": <int>,
    "dairy-free": <int>,
    "nut-free": <int>,
    "pescatarian": <int>,
    }},
  "halal_mentioned": <true if halal is mentioned anywhere on the site, false otherwise>,
  "confidence": "high" | "medium" | "low" | "no_menu",
  "reccomended_dishes": <names of the two dishes that we want to reccomend seperated by commas>,
  "notes": "<any caveats>"
}}"""


CUISINE_DETECTION_ADDENDUM = """

### Additional Task: Cuisine Detection
The cuisine type for this restaurant is unknown. Based on the website content, menu items, restaurant name, and any other signals, determine the cuisine type(s).

1. **Primary cuisine**: The single best-fitting cuisine label.
2. **Cuisine scores**: A dictionary of ALL cuisines that apply, each with a confidence score from 0 to 1. The primary cuisine should always be 1.0. Include a secondary cuisine (and any others) if the restaurant partially fits — e.g. a Mexican restaurant with Spanish influences might be {{"Mexican": 1.0, "Spanish": 0.7}}.

Use specific labels such as: "Italian", "Japanese", "Mexican", "Indian", "Chinese", "Thai", "French", "American", "Mediterranean", "Korean", "Vietnamese", "Turkish", "Ethiopian", "Peruvian", "British", "Greek", "Spanish", "Middle Eastern", "Caribbean", "Fusion", etc.

Add these to your JSON output as:
  "detected_cuisine": "<primary cuisine label>",
  "cuisine_scores": {{"<cuisine>": <float 0-1>, ...}}
"""


async def analyse_with_llm(
    content: str,
    client: AsyncOpenAI,
    restaurant_name: str = "",
    cuisine_hint: str = "",
    menu_images: Optional[List[Tuple[str, str, str]]] = None,
    detect_cuisine: bool = False,
) -> dict:
    """Send menu content to xAI Grok for dietary analysis."""
    dietary_list = "\n".join(f"- {d}" for d in DIETARY_REQUIREMENTS)

    prompt = ANALYSIS_PROMPT.format(
        content=content,
        dietary_list=dietary_list,
        restaurant_name=restaurant_name or "Unknown",
        cuisine_hint=cuisine_hint or "Unknown",
    )

    if detect_cuisine:
        prompt += CUISINE_DETECTION_ADDENDUM

    if menu_images:
        message_content: list = []
        for _url, b64_data, media_type in menu_images:
            message_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{media_type};base64,{b64_data}",
                },
            })
        image_note = (
            f"\n\nNOTE: {len(menu_images)} menu image(s) are attached above. "
            f"These are photographs of the restaurant's physical menu. "
            f"Read ALL text visible in the images to extract dish names, descriptions, and prices. "
            f"Use this information for the dietary analysis.\n"
        )
        message_content.append({"type": "text", "text": prompt + image_note})
    else:
        message_content = prompt  # type: ignore[assignment]

    response = await client.chat.completions.create(
        model=MODEL,
        max_tokens=1500,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You are a restaurant menu analyst. Always respond with valid JSON."},
            {"role": "user", "content": message_content},
        ],
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Failed to parse LLM response: %s", raw[:200])
        return {
            "description": "",
            "total_dishes": 0,
            "dietary_counts": {d: 0 for d in DIETARY_REQUIREMENTS},
            "confidence": "low",
            "notes": "LLM response parsing failed",
        }


# ── Single Restaurant Pipeline ───────────────────────────────────────────────

async def _analyse_restaurant(
    website: str,
    google_place_id: str,
    crawler: AsyncWebCrawler,
    client: AsyncOpenAI,
    restaurant_name: str = "",
    cuisine_hint: str = "",
    detect_cuisine: bool = False,
) -> MenuAnalysisResult:
    """End-to-end analysis for a single restaurant."""
    result = MenuAnalysisResult(
        google_place_id=google_place_id,
        website=website,
    )

    if not website.startswith("http"):
        website = "https://" + website

    try:
        # 1. Discover menu page
        menu_url, homepage_md = await discover_menu_url(website, crawler)

        # 2. Extract menu content
        menu_md = ""
        menu_images: List[Tuple[str, str, str]] = []
        if menu_url:
            result.menu_url = menu_url
            result.menu_found = True

            menu_is_homepage = menu_url.rstrip("/").lower() == website.rstrip("/").lower()

            if menu_is_homepage:
                menu_md = homepage_md
                pdf_results = await extract_pdfs_from_markdown(menu_md, menu_url)
                if pdf_results:
                    _, pdf_text = pdf_results[0]
                    menu_md += f"\n\n### PDF Menu\n{pdf_text[:5000]}"
                else:
                    menu_images = await download_menu_images(menu_md, menu_url)
            else:
                extracted_text, extracted_images = await extract_menu_content(menu_url, crawler)
                menu_md = extracted_text or ""
                menu_images = extracted_images

        # Combine homepage + menu page
        combined = ""
        if homepage_md:
            combined += f"## Homepage Content\n{homepage_md[:5000]}\n\n"
        if menu_md:
            combined += f"## Menu Page Content\n{menu_md}\n\n"
            result.menu_markdown = menu_md

        if not combined or len(combined.strip()) < 100:
            combined = (
                f"No website content could be extracted.\n"
                f"Restaurant name: {restaurant_name or 'Unknown'}\n"
                f"Known cuisine: {cuisine_hint or 'Unknown'}\n"
            )

        # 3. LLM analysis
        analysis = await analyse_with_llm(
            combined, client,
            restaurant_name=restaurant_name,
            cuisine_hint=cuisine_hint,
            menu_images=menu_images,
            detect_cuisine=detect_cuisine,
        )

        result.menu_url = analysis.get("menu_url", result.menu_url)
        result.restaurant_description = analysis.get("description", "")
        result.total_dishes = analysis.get("total_dishes", 0)
        result.confidence = analysis.get("confidence", "low")
        result.reccomended_dishes = analysis.get("reccomended_dishes", None)
        result.detected_cuisine = analysis.get("detected_cuisine", None)
        result.cuisine_scores = analysis.get("cuisine_scores", None)
        result.notes = analysis.get("notes", "")

        # 4. Compute dietary scores
        dietary_counts = analysis.get("dietary_counts", {})
        halal_mentioned = analysis.get("halal_mentioned", False)
        result.halal_mentioned = halal_mentioned

        pescatarian_pct = 0
        for req in DIETARY_REQUIREMENTS:
            count = dietary_counts.get(req, 0)
            pct = DietaryScore.compute_pct(count)
            if req == "pescatarian":
                pescatarian_pct = pct
            result.dietary_scores.append(DietaryScore(
                requirement=req,
                dish_count=count,
                friendliness_pct=pct,
            ))

        # Halal: 100% if mentioned, otherwise fallback to pescatarian score
        halal_count = dietary_counts.get("halal", 0)
        halal_pct = 100 if halal_mentioned else pescatarian_pct
        result.dietary_scores.append(DietaryScore(
            requirement="halal",
            dish_count=halal_count,
            friendliness_pct=halal_pct,
        ))

    except Exception as e:
        logger.error("Pipeline error for %s: %s", website, e, exc_info=True)
        result.error = str(e)

    return result


# ── Top-Level Public Function ────────────────────────────────────────────────

async def process_menu_for_location(
    location_id: int,
    google_place_id: str,
    website: str,
    restaurant_name: str = "",
    cuisine_hint: str = "",
    detect_cuisine: bool = False,
) -> Optional[MenuAnalysisResult]:
    """
    Crawl a restaurant's website, analyse the menu, and update Supabase.

    This is the main entry point for menu processing. It:
    1. Discovers the menu page on the website
    2. Extracts content (HTML, PDF, images)
    3. Uses xAI Grok to analyse dietary friendliness and generate a description
    4. Updates the location in Supabase with menu data and dietary_requirement_vector
    5. Optionally detects cuisine type if detect_cuisine=True

    Args:
        location_id: The Pinit location ID in Supabase.
        google_place_id: Google Place ID for the restaurant.
        website: Restaurant website URL.
        restaurant_name: Name of the restaurant (for LLM context).
        cuisine_hint: Known cuisine type (for LLM context).
        detect_cuisine: If True, ask the LLM to detect the cuisine type and
            update cuisine_primary on the location.

    Returns:
        MenuAnalysisResult on success, None on fatal error.
    """
    if not XAI_API_KEY:
        logger.error("XAI_API_KEY not configured, skipping menu processing for location %s", location_id)
        return None

    logger.info(
        "Starting menu processing for location %s (%s) — website: %s",
        location_id, restaurant_name, website,
    )

    client = AsyncOpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL)

    browser_config = BrowserConfig(
        headless=True,
        viewport_width=1280,
        viewport_height=800,
    )

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await _analyse_restaurant(
                website=website,
                google_place_id=google_place_id,
                crawler=crawler,
                client=client,
                restaurant_name=restaurant_name,
                cuisine_hint=cuisine_hint,
                detect_cuisine=detect_cuisine,
            )
    except Exception as e:
        logger.error("Menu processing failed for location %s: %s", location_id, e, exc_info=True)
        return None

    if result.error:
        logger.warning("Menu analysis had error for location %s: %s", location_id, result.error)

    # Update the location in Supabase
    try:
        supabase = get_supabase_service()

        # Update location fields
        update_data: Dict[str, object] = {
            "menu_analysis_confidence": result.confidence,
        }
        if result.menu_url:
            update_data["menu"] = result.menu_url
        if result.restaurant_description:
            update_data["generated_summary"] = result.restaurant_description
        if result.reccomended_dishes:
            update_data["reccomended_dishes"] = result.reccomended_dishes
        if detect_cuisine and result.detected_cuisine:
            update_data["cuisine_primary"] = result.detected_cuisine
            if result.cuisine_scores:
                update_data["cuisine_scores_json"] = result.cuisine_scores
                # Set cuisine_secondary to the highest-scoring cuisine that isn't the primary
                secondary = max(
                    (
                        (cuisine, score)
                        for cuisine, score in result.cuisine_scores.items()
                        if cuisine != result.detected_cuisine
                    ),
                    key=lambda x: x[1],
                    default=None,
                )
                if secondary and secondary[1] > 0.3:
                    update_data["cuisine_secondary"] = secondary[0]

        supabase.update_location(location_id, **update_data)
        logger.info("Updated location %s with menu analysis fields", location_id)

        # Build dietary_requirement_vector from scores
        dietary_order = supabase.dietary_requirements_order
        vector = [0] * len(dietary_order)
        for ds in result.dietary_scores:
            idx = dietary_order.get(ds.requirement)
            if idx is not None:
                vector[idx] = ds.friendliness_pct

        supabase.update_location(location_id, dietary_requirement_vector=vector)
        logger.info(
            "Updated dietary_requirement_vector for location %s: %s",
            location_id, vector,
        )

    except Exception as e:
        logger.error(
            "Failed to update Supabase for location %s after menu analysis: %s",
            location_id, e, exc_info=True,
        )

    logger.info(
        "Menu processing complete for location %s — menu_found=%s, confidence=%s, total_dishes=%d",
        location_id, result.menu_found, result.confidence, result.total_dishes,
    )

    return result
