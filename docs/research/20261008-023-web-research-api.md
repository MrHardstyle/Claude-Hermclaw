# 20261008-023 – API für Web-Research (Suche)

## Question
Welche Such-API nutzt die Research Engine?

## Sources
- LiteLLM SearXNG-Doku: https://docs.litellm.ai/docs/search/searxng
- Open WebUI SearXNG: https://docs.openwebui.com/features/chat-conversations/web-search/providers/searxng.md
- SearXNG Settings `search.formats`: https://docs.searxng.org/admin/settings/settings_search.html

## Relevant facts
- SearXNG liefert JSON nur, wenn `search.formats` `json` enthält; Endpoint `GET /search?q=…&format=json`; sonst 403/HTML.
- Selbst gehostet, keine API-Kosten, keine Schlüssel.

## Rejected alternatives
Kommerzielle Such-APIs (Brave/Bing/Google CSE) – möglich als Provider, aber Schlüssel und Kosten; nicht Standard.

## Decision (offen gelassen durch Bauplan → DECISIONS.md D-004)
SearXNG als Standard-Provider (Container auf `.225`, nur intern erreichbar); Provider-Interface erlaubt weitere.

## Implementation consequences
- `SearxngSearchProvider` (httpx), Abruf mit `HttpFetcher` (Größenlimit, Content-Type-Filter, robots-freundlicher User-Agent), Extraktion HTML→Text mit BeautifulSoup/lxml.
- Primärquellen-Bewertung per konfigurierbarer Domainliste (`policies.yaml: research.primary_domains`).
