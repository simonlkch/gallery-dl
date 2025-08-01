# -*- coding: utf-8 -*-

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://www.fanbox.cc/"""

from .common import Extractor, Message
from .. import text, util
from ..cache import memcache

BASE_PATTERN = r"(?:https?://)?(?:www\.)?fanbox\.cc"
USER_PATTERN = (
    r"(?:https?://)?(?:"
    r"(?!www\.)([\w-]+)\.fanbox\.cc|"
    r"(?:www\.)?fanbox\.cc/@([\w-]+))"
)


class FanboxExtractor(Extractor):
    """Base class for Fanbox extractors"""
    category = "fanbox"
    root = "https://www.fanbox.cc"
    directory_fmt = ("{category}", "{creatorId}")
    filename_fmt = "{id}_{num}.{extension}"
    archive_fmt = "{id}_{num}"
    browser = "firefox"
    _warning = True

    def _init(self):
        self.headers = {
            "Accept" : "application/json, text/plain, */*",
            "Origin" : "https://www.fanbox.cc",
            "Referer": "https://www.fanbox.cc/",
            "Cookie" : None,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        self.embeds = self.config("embeds", True)

        if includes := self.config("metadata"):
            if isinstance(includes, str):
                includes = includes.split(",")
            elif not isinstance(includes, (list, tuple)):
                includes = ("user", "plan")
            self._meta_user = ("user" in includes)
            self._meta_plan = ("plan" in includes)
            self._meta_comments = ("comments" in includes)
        else:
            self._meta_user = self._meta_plan = self._meta_comments = False

        if self.config("comments"):
            self._meta_comments = True

        if self._warning:
            if not self.cookies_check(("FANBOXSESSID",)):
                self.log.warning("no 'FANBOXSESSID' cookie set")
            FanboxExtractor._warning = False

    def items(self):
        fee_max = self.config("fee-max")

        for item in self.posts():
            if fee_max is not None and fee_max < item["feeRequired"]:
                self.log.warning("Skipping post %s (feeRequired of %s > %s)",
                                 item["id"], item["feeRequired"], fee_max)
                continue

            try:
                url = "https://api.fanbox.cc/post.info?postId=" + item["id"]
                body = self.request_json(url, headers=self.headers)["body"]
                content_body, post = self._extract_post(body)
            except Exception as exc:
                self.log.warning("Skipping post %s (%s: %s)",
                                 item["id"], exc.__class__.__name__, exc)
                continue

            yield Message.Directory, post
            yield from self._get_urls_from_post(content_body, post)

    def posts(self):
        """Return all relevant post objects"""

    def _pagination(self, url):
        while url:
            url = text.ensure_http_scheme(url)
            body = self.request_json(url, headers=self.headers)["body"]

            yield from body["items"]

            url = body["nextUrl"]

    def _extract_post(self, post):
        """Fetch and process post data"""
        post["archives"] = ()

        if content_body := post.pop("body", None):
            if "html" in content_body:
                post["html"] = content_body["html"]
            if post["type"] == "article":
                post["articleBody"] = content_body.copy()
            if "blocks" in content_body:
                content = []  # text content
                images = []   # image IDs in 'body' order
                files = []    # file IDs in 'body' order

                for block in content_body["blocks"]:
                    if "text" in block:
                        content.append(block["text"])
                    if "links" in block:
                        for link in block["links"]:
                            content.append(link["url"])
                    if "imageId" in block:
                        images.append(block["imageId"])
                    if "fileId" in block:
                        files.append(block["fileId"])

                post["content"] = "\n".join(content)

                self._sort_map(content_body, "imageMap", images)
                if file_map := self._sort_map(content_body, "fileMap", files):
                    exts = util.EXTS_ARCHIVE
                    post["archives"] = [
                        file
                        for file in file_map.values()
                        if file.get("extension", "").lower() in exts
                    ]

        post["date"] = text.parse_datetime(post["publishedDatetime"])
        post["text"] = content_body.get("text") if content_body else None
        post["isCoverImage"] = False

        if self._meta_user:
            post["user"] = self._get_user_data(post["creatorId"])
        if self._meta_plan:
            plans = self._get_plan_data(post["creatorId"])
            fee = post["feeRequired"]
            try:
                post["plan"] = plans[fee]
            except KeyError:
                if fees := [f for f in plans if f >= fee]:
                    plan = plans[min(fees)]
                else:
                    plan = plans[0].copy()
                    plan["fee"] = fee
                post["plan"] = plans[fee] = plan
        if self._meta_comments:
            if post["commentCount"]:
                post["comments"] = list(self._get_comment_data(post["id"]))
            else:
                post["commentd"] = ()

        return content_body, post

    def _sort_map(self, body, key, ids):
        orig = body.get(key)
        if not orig:
            return {} if orig is None else orig

        body[key] = new = {
            id: orig[id]
            for id in ids
            if id in orig
        }

        return new

    @memcache(keyarg=1)
    def _get_user_data(self, creator_id):
        url = "https://api.fanbox.cc/creator.get"
        params = {"creatorId": creator_id}
        data = self.request_json(url, params=params, headers=self.headers)

        user = data["body"]
        user.update(user.pop("user"))

        return user

    @memcache(keyarg=1)
    def _get_plan_data(self, creator_id):
        url = "https://api.fanbox.cc/plan.listCreator"
        params = {"creatorId": creator_id}
        data = self.request_json(url, params=params, headers=self.headers)

        plans = {0: {
            "id"             : "",
            "title"          : "",
            "fee"            : 0,
            "description"    : "",
            "coverImageUrl"  : "",
            "creatorId"      : creator_id,
            "hasAdultContent": None,
            "paymentMethod"  : None,
        }}
        for plan in data["body"]:
            del plan["user"]
            plans[plan["fee"]] = plan

        return plans

    def _get_comment_data(self, post_id):
        url = ("https://api.fanbox.cc/post.getComments"
               "?limit=10&postId=" + post_id)

        comments = []
        while url:
            url = text.ensure_http_scheme(url)
            body = self.request_json(url, headers=self.headers)["body"]
            data = body["commentList"]
            comments.extend(data["items"])
            url = data["nextUrl"]
        return comments

    def _get_urls_from_post(self, content_body, post):
        num = 0
        if cover_image := post.get("coverImageUrl"):
            cover_image = util.re("/c/[0-9a-z_]+").sub("", cover_image)
            final_post = post.copy()
            final_post["isCoverImage"] = True
            final_post["fileUrl"] = cover_image
            text.nameext_from_url(cover_image, final_post)
            final_post["num"] = num
            num += 1
            yield Message.Url, cover_image, final_post

        if not content_body:
            return

        if "html" in content_body:
            html_urls = []

            for href in text.extract_iter(content_body["html"], 'href="', '"'):
                if "fanbox.pixiv.net/images/entry" in href:
                    html_urls.append(href)
                elif "downloads.fanbox.cc" in href:
                    html_urls.append(href)
            for src in text.extract_iter(content_body["html"],
                                         'data-src-original="', '"'):
                html_urls.append(src)

            for url in html_urls:
                final_post = post.copy()
                text.nameext_from_url(url, final_post)
                final_post["fileUrl"] = url
                final_post["num"] = num
                num += 1
                yield Message.Url, url, final_post

        for group in ("images", "imageMap"):
            if group in content_body:
                for item in content_body[group]:
                    if group == "imageMap":
                        # imageMap is a dict with image objects as values
                        item = content_body[group][item]

                    final_post = post.copy()
                    final_post["fileUrl"] = item["originalUrl"]
                    text.nameext_from_url(item["originalUrl"], final_post)
                    if "extension" in item:
                        final_post["extension"] = item["extension"]
                    final_post["fileId"] = item.get("id")
                    final_post["width"] = item.get("width")
                    final_post["height"] = item.get("height")
                    final_post["num"] = num
                    num += 1
                    yield Message.Url, item["originalUrl"], final_post

        for group in ("files", "fileMap"):
            if group in content_body:
                for item in content_body[group]:
                    if group == "fileMap":
                        # fileMap is a dict with file objects as values
                        item = content_body[group][item]

                    final_post = post.copy()
                    final_post["fileUrl"] = item["url"]
                    text.nameext_from_url(item["url"], final_post)
                    if "extension" in item:
                        final_post["extension"] = item["extension"]
                    if "name" in item:
                        final_post["filename"] = item["name"]
                    final_post["fileId"] = item.get("id")
                    final_post["num"] = num
                    num += 1
                    yield Message.Url, item["url"], final_post

        if self.embeds:
            embeds_found = []
            if "video" in content_body:
                embeds_found.append(content_body["video"])
            embeds_found.extend(content_body.get("embedMap", {}).values())

            for embed in embeds_found:
                # embed_result is (message type, url, metadata dict)
                embed_result = self._process_embed(post, embed)
                if not embed_result:
                    continue
                embed_result[2]["num"] = num
                num += 1
                yield embed_result

    def _process_embed(self, post, embed):
        final_post = post.copy()
        provider = embed["serviceProvider"]
        content_id = embed.get("videoId") or embed.get("contentId")
        prefix = "ytdl:" if self.embeds == "ytdl" else ""
        url = None
        is_video = False

        if provider == "soundcloud":
            url = prefix+"https://soundcloud.com/"+content_id
            is_video = True
        elif provider == "youtube":
            url = prefix+"https://youtube.com/watch?v="+content_id
            is_video = True
        elif provider == "vimeo":
            url = prefix+"https://vimeo.com/"+content_id
            is_video = True
        elif provider == "fanbox":
            # this is an old URL format that redirects
            # to a proper Fanbox URL
            url = "https://www.pixiv.net/fanbox/"+content_id
            # resolve redirect
            try:
                url = self.request_location(url)
            except Exception as exc:
                url = None
                self.log.warning("Unable to extract fanbox embed %s (%s: %s)",
                                 content_id, exc.__class__.__name__, exc)
            else:
                final_post["_extractor"] = FanboxPostExtractor
        elif provider == "twitter":
            url = "https://twitter.com/_/status/"+content_id
        elif provider == "google_forms":
            url = (f"https://docs.google.com/forms/d/e/"
                   f"{content_id}/viewform?usp=sf_link")
        else:
            self.log.warning(f"service not recognized: {provider}")

        if url:
            final_post["embed"] = embed
            final_post["embedUrl"] = url
            text.nameext_from_url(url, final_post)
            msg_type = Message.Queue
            if is_video and self.embeds == "ytdl":
                msg_type = Message.Url
            return msg_type, url, final_post


class FanboxCreatorExtractor(FanboxExtractor):
    """Extractor for a Fanbox creator's works"""
    subcategory = "creator"
    pattern = USER_PATTERN + r"(?:/posts)?/?$"
    example = "https://USER.fanbox.cc/"

    def posts(self):
        url = "https://api.fanbox.cc/post.paginateCreator?creatorId="
        creator_id = self.groups[0] or self.groups[1]
        return self._pagination_creator(url + creator_id)

    def _pagination_creator(self, url):
        urls = self.request_json(url, headers=self.headers)["body"]
        for url in urls:
            url = text.ensure_http_scheme(url)
            yield from self.request_json(url, headers=self.headers)["body"]


class FanboxPostExtractor(FanboxExtractor):
    """Extractor for media from a single Fanbox post"""
    subcategory = "post"
    pattern = USER_PATTERN + r"/posts/(\d+)"
    example = "https://USER.fanbox.cc/posts/12345"

    def posts(self):
        return ({"id": self.groups[2], "feeRequired": 0},)


class FanboxHomeExtractor(FanboxExtractor):
    """Extractor for your Fanbox home feed"""
    subcategory = "home"
    pattern = BASE_PATTERN + r"/?$"
    example = "https://fanbox.cc/"

    def posts(self):
        url = "https://api.fanbox.cc/post.listHome?limit=10"
        return self._pagination(url)


class FanboxSupportingExtractor(FanboxExtractor):
    """Extractor for your supported Fanbox users feed"""
    subcategory = "supporting"
    pattern = BASE_PATTERN + r"/home/supporting"
    example = "https://fanbox.cc/home/supporting"

    def posts(self):
        url = "https://api.fanbox.cc/post.listSupporting?limit=10"
        return self._pagination(url)


class FanboxRedirectExtractor(Extractor):
    """Extractor for pixiv redirects to fanbox.cc"""
    category = "fanbox"
    subcategory = "redirect"
    pattern = r"(?:https?://)?(?:www\.)?pixiv\.net/fanbox/creator/(\d+)"
    example = "https://www.pixiv.net/fanbox/creator/12345"

    def items(self):
        url = "https://www.pixiv.net/fanbox/creator/" + self.groups[0]
        location = self.request_location(url, notfound="user")
        yield Message.Queue, location, {"_extractor": FanboxCreatorExtractor}
