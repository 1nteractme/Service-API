import os


BOT_NAME = "service_scraper"

SPIDER_MODULES = ["service_scraper.spiders"]
NEWSPIDER_MODULE = "service_scraper.spiders"

ROBOTSTXT_OBEY = False
CONCURRENT_REQUESTS = 1
DOWNLOAD_TIMEOUT = 60
TELNETCONSOLE_ENABLED = False
EXTENSIONS = {
    "scrapy.extensions.remote_control.RemoteControl": None,
}

TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}

PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 60_000
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": os.getenv("SERVICE_HEADLESS", "1") != "0",
}

FEED_EXPORT_ENCODING = "utf-8"
LOG_LEVEL = os.getenv("SCRAPY_LOG_LEVEL", "INFO")
