import re
from itertools import chain
from typing import Any, Callable, NamedTuple, Optional, Union
from urllib.error import HTTPError

from atproto import Client
from atproto_client.models import AppBskyFeedSearchPosts
from dateutil.parser import ParserError
from dateutil.parser import parse as attempt_to_parse_date
from easyocr import Reader as ImageReader

REPLIES = "replies"
RETWEETS = "retweets"
QUOTES = "quotes"
BOOKMARKS = "bookmarks"
LIKES = "likes"


class ExpectedPostCharacteristicInfo(NamedTuple):
    regex: Optional[str]
    position_threshold: int
    from_end: bool


class TweetCompiler:
    max_pages: int = 100
    # Within how many positions in the list of extracted image texts we expect to
    # see a desired piece of text (e.g., person's handle or like count)
    position_threshold = 5
    # threshold to pass to the image processing library
    ocr_probability_threshold: float = 0.5
    # if we find at least this proportion of the expected pieces of a screeshot's
    # appearance, assume we found a post
    post_characteristics_probability_threshold: float = 0.25

    # For searching Bluesky
    hashtags: list[str]
    accounts: list[str]
    # What we expect to see on a twitter/X or untruth social screenshot, respectively
    twix_user_full_name: Optional[str]
    twix_user_handle: Optional[str]
    untruth_social_user_full_name: Optional[str]
    untruth_social_user_handle: Optional[str]

    def __init__(self, bksy_client: Client):
        self.client = bksy_client
        self.image_reader = ImageReader(["en"])
        if self.hashtags is None and self.accounts is None:
            raise ValueError(
                "Must specify one or both of hashtags and accounts to search."
            )
        self.untruth_social_generics = self.get_untruth_social_generics()
        self.twix_generics = self.get_twix_generics()

    def get_untruth_social_generics(self) -> dict[str, ExpectedPostCharacteristicInfo]:
        if (
            not self.untruth_social_user_full_name
            or not self.untruth_social_user_handle
        ):
            return {}

        return {
            "Truth Details": ExpectedPostCharacteristicInfo(
                regex=None,
                position_threshold=self.position_threshold,
                from_end=False,
            ),
            REPLIES: ExpectedPostCharacteristicInfo(
                regex=".* (r|R)epl(y|ies)$",
                position_threshold=self.position_threshold + 1,
                from_end=False,
            ),
            self.untruth_social_user_full_name: ExpectedPostCharacteristicInfo(
                regex=None,
                position_threshold=self.position_threshold + 2,
                from_end=False,
            ),
            self.untruth_social_user_handle: ExpectedPostCharacteristicInfo(
                regex=None,
                position_threshold=self.position_threshold + 3,
                from_end=False,
            ),
            RETWEETS: ExpectedPostCharacteristicInfo(
                regex=".* ReTruths?$",
                position_threshold=self.position_threshold + 1,
                # re-untruths and likes are typically near bottom of image as opposed to top
                from_end=True,
            ),
            LIKES: ExpectedPostCharacteristicInfo(
                regex=".* (l|L)ikes?$",
                position_threshold=self.position_threshold,
                from_end=True,
            ),
        }

    def get_twix_generics(self) -> dict[str, ExpectedPostCharacteristicInfo]:
        if not self.twix_user_full_name or not self.twix_user_handle:
            return {}

        return {
            self.twix_user_full_name: ExpectedPostCharacteristicInfo(
                regex=None,
                position_threshold=self.position_threshold,
                from_end=False,
            ),
            self.twix_user_handle: ExpectedPostCharacteristicInfo(
                regex=None,
                position_threshold=self.position_threshold + 1,
                from_end=False,
            ),
            # Reposts, quotes, likes, and/or bookmarks seem to appear at bottom
            # of screenshots if indeed they made it into the crop
            # DOUBLE CHECK THIS PROBABLY - are they actually separate list
            # items when OCR spits them out for us??
            RETWEETS: ExpectedPostCharacteristicInfo(
                regex=".* Reposts?$",
                position_threshold=self.position_threshold + 3,
                from_end=True,
            ),
            QUOTES: ExpectedPostCharacteristicInfo(
                regex=".* Quotes?$",
                position_threshold=self.position_threshold + 2,
                from_end=True,
            ),
            LIKES: ExpectedPostCharacteristicInfo(
                regex=".* Likes?$",
                position_threshold=self.position_threshold + 1,
                from_end=True,
            ),
            BOOKMARKS: ExpectedPostCharacteristicInfo(
                regex=".* Bookmarks?$",
                position_threshold=self.position_threshold,
                from_end=True,
            ),
        }

    def _within_n_positions(
        self,
        word_group: str,
        word_group_to_index: dict[str, int],
        n: Optional[int] = None,
        from_end: bool = False,  # within last n instead of within first n
    ) -> bool:
        """
        determine if word_group is within n positions from front or back of
        a list based on pre-computed mapping of word_group to its index in the list
        """
        if n is None:
            n = self.position_threshold
        if word_group not in word_group_to_index:
            return False
        if not from_end:
            return word_group_to_index[word_group] < n
        return len(word_group_to_index) - word_group_to_index[word_group] <= n

    def is_probably_their_platform_post(
        self,
        extracted_image_texts: list[str],
        platform_generics: dict[str, ExpectedPostCharacteristicInfo],
        probability_threshold: Optional[float] = None,
    ) -> bool:
        if probability_threshold is None:
            probability_threshold = self.post_characteristics_probability_threshold

        word_group_positions = {}
        # cumbersome but we especially dont want to overwrite the indexes of these
        # if somehow they magically appear later in a post body:
        regex_characteristics_found = {
            name: False for name, info in platform_generics.items() if info.regex
        }
        for index, word_group in enumerate(extracted_image_texts):
            # stuff with numbers that needs to be normalized (in giant air quotes)
            for characteristic_name, characteristic in platform_generics.items():
                if (
                    characteristic.regex
                    and not regex_characteristics_found[characteristic_name]
                    and re.match(characteristic.regex, word_group.strip())
                ):
                    word_group_positions[characteristic_name] = index
                    regex_characteristics_found[characteristic_name] = True
            # everything else
            if word_group not in word_group_positions:
                word_group_positions[word_group] = index
            # i really don't expect this to happen but i also don't want some later
            # text than somehow matches an expected intro item to override the index
            else:
                word_group_positions[f"{word_group}*"] = index

        # We think it is probably a post from the given plaform if it has certain
        # introductory and/or footer material within the first or last few indexes,
        # respectively, of the extracted image text list
        # screenshot crops could vary a lot, so probability threshold should not be too high
        probability_points = 0
        total_points = 0
        for post_characteristic, expected_info in platform_generics.items():
            if self._within_n_positions(
                post_characteristic,
                word_group_positions,
                n=expected_info.position_threshold,
                from_end=expected_info.from_end,
            ):
                probability_points += 1
            total_points += 1

        return (
            probability_points / total_points >= probability_threshold
            if total_points
            else False
        )

    def is_probably_their_twix_post(self, extracted_image_texts: list[str]) -> bool:
        """
        If you know the person is on Twitter/X, call this with
        their name and handle

        Use what we know about person's account screenshot appearance to
        decide if we have found an image of one of their tweets
        """
        if not self.twix_user_full_name or not self.twix_user_handle:
            return False

        return self.is_probably_their_platform_post(
            extracted_image_texts,
            self.twix_generics,
        )

    def is_probably_their_untruth_social_post(
        self, extracted_image_texts: list[str]
    ) -> bool:
        """
        If you know the person is on Untruth Social, call this with
        their name, handle, etc.

        Use what we know about person's account screenshot appearance to
        decide if we have found an image of one of their Untruth Social posts
        """
        if (
            not self.untruth_social_user_full_name
            or not self.untruth_social_user_handle
        ):
            return False

        return self.is_probably_their_platform_post(
            extracted_image_texts,
            self.untruth_social_generics,
        )

    def is_probably_their_tweet(self, extracted_image_texts: list[str]) -> bool:
        return self.is_probably_their_untruth_social_post(
            extracted_image_texts,
        ) or self.is_probably_their_twix_post(
            extracted_image_texts,
        )

    def clean_extracted_texts(self, extracted_texts: list[str]) -> list[str]:
        # TODO: finish removing "Trending", numbers, dates, and timestamps
        # stuff like 3/4/25,10:13 AM is still common
        cleaned_texts = []
        for text in extracted_texts:
            for identifier, info in chain(
                self.untruth_social_generics.items(), self.twix_generics.items()
            ):
                if info.regex:
                    text = re.sub(info.regex.rstrip("$"), "", text.strip())
                else:
                    text = text.replace(identifier, "")

            # Attempt to filter out dates, but only try if there is a number
            if re.search("\d", text):
                try:
                    its_just_a_number = float(text)
                except (ValueError, OverflowError):
                    pass
                else:
                    if its_just_a_number:
                        continue

                try:
                    # dateutil is AMAZING but it does not like the AM/PM apparently
                    maybe_a_date = attempt_to_parse_date(
                        re.sub("am|pm", "", text.lower())
                    )
                except (ParserError, OverflowError):
                    pass
                else:
                    if maybe_a_date:
                        continue
            if text:
                cleaned_texts.append(text)

        return cleaned_texts

    def get_tweet_text_if_confident(self, image_url: str) -> Union[str, None]:
        extracted_texts = []
        try:
            reader_output = self.image_reader.readtext(image_url)
        except HTTPError:
            return None

        for _, text, confidence_level in reader_output:
            if confidence_level >= self.ocr_probability_threshold:
                extracted_texts.append(text)

        if self.is_probably_their_tweet(extracted_texts):
            cleaned_extracted_texts = self.clean_extracted_texts(extracted_texts)
            return " ".join(cleaned_extracted_texts)
        return None

    def _get_tweets_list_from_api_call(
        self, getter_func: Callable, *getter_args, **getter_kwargs
    ) -> list[str]:
        response = getter_func(*getter_args, **getter_kwargs)
        feed_response = getattr(response, "feed", None)
        posts_response = getattr(response, "posts", None)
        if feed_response:
            items = feed_response
            post_attr = "post"
            cursor_in_params = False
        elif posts_response:
            items = posts_response
            # each thing we itereate is already a post
            # I want to use None but it makes mypy yell so something here, have
            # something falsy that won't make mypy yell 😣:
            post_attr = ""
            cursor_in_params = True
        else:
            raise ValueError(f"Unkown API response format: response ({type(response)})")

        tweets_list = []
        pages_seen = 0
        while pages_seen <= self.max_pages:
            for item in items:  # for post in response.posts
                post = getattr(item, post_attr) if post_attr else item
                if post and post.embed and hasattr(post.embed, "images"):
                    for image in post.embed.images:
                        tweet_text = self.get_tweet_text_if_confident(image.fullsize)
                        if tweet_text is not None:
                            tweets_list.append(tweet_text)

            pages_seen += 1
            next_page = response.cursor
            if next_page:
                if cursor_in_params:
                    getter_kwargs["params"].cursor = next_page
                else:
                    getter_kwargs["cursor"] = next_page
                response = getter_func(*getter_args, **getter_kwargs)
            else:
                break

        return tweets_list

    def get_tweets_list_from_account(self, account: str) -> list[str]:
        return self._get_tweets_list_from_api_call(self.client.get_author_feed, account)

    def get_tweets_list_from_hashtag(self, hashtag: str) -> list[str]:
        # There is a tag parameter but it does not seem to work:
        # https://www.reddit.com/r/BlueskySocial/comments/1h00922/trying_to_query_api_programmatically_cant_search/
        return self._get_tweets_list_from_api_call(
            self.client.app.bsky.feed.search_posts,
            params=AppBskyFeedSearchPosts.Params(
                q=f"#{hashtag}",
                sort="latest",
            ),
        )

    def get_all_tweets(self) -> list[str]:
        all_tweets = []
        for hashtag in self.hashtags:
            all_tweets.extend(self.get_tweets_list_from_hashtag(hashtag))
        for account in self.accounts:
            all_tweets.extend(self.get_tweets_list_from_account(account))
        return all_tweets


class TrumpTweetCompiler(TweetCompiler):
    hashtags = [
        "TrumpTweets",
        "TrumpTweet",
        "trumptweets",
        "trumptweet",
        "TheresAlwaysATweet",
    ]
    accounts = ["trumptweets.bsky.social", "trumpwatch.skyfleet.blue"]
    twix_user_full_name = "Donald J. Trump"
    twix_user_handle = "@realDonaldTrump"
    untruth_social_user_full_name = "Donald J. Trump"
    untruth_social_user_handle = "@realDonaldTrump"


TWEET_COMPILER_CLASSES = [
    TrumpTweetCompiler,
]
