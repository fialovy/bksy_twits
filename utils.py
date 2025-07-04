import random
import re
from itertools import chain
from typing import Any, Callable, NamedTuple, Optional, Union
from urllib.error import HTTPError

from atproto import Client
from atproto_client.models import AppBskyFeedSearchPosts
from dateutil.parser import ParserError
from dateutil.parser import parse as attempt_to_parse_date
from easyocr import Reader as ImageReader
from fuzzywuzzy import fuzz

from villain_quotes import VILLAIN_QUOTES

REPLIES = "replies"
RETWEETS = "retweets"
QUOTES = "quotes"
BOOKMARKS = "bookmarks"
LIKES = "likes"

OTHER_REGEXES_TO_CLEAN = frozenset(
    [
        r"Twitter for [a-zA-Z]+",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\s*,?\s*\d{1,2}:\d{2}\s*(?:AM|PM)\b",
    ]
)

QUOTE_INSERTION_WINDOW_SIZE = 5
TWEET_DEDUPE_FUZZY_MATCH_THRESHOLD = 80

MARKOVIFY_STATE_SIZE = (
    2  # this is the default and sadly any more isn't giving me anything yet
)
MARKOVIFY_MAX_TRIES = 1000


class ExpectedPostCharacteristicInfo(NamedTuple):
    regex: Optional[str]
    position_threshold: int
    from_end: bool


class TweetCompiler:
    nickname = "bot"
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
    twix_user_full_name: Optional[str] = None
    twix_user_handle: Optional[str] = None
    untruth_social_user_full_name: Optional[str] = None
    untruth_social_user_handle: Optional[str] = None

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

            for other_regex in OTHER_REGEXES_TO_CLEAN:
                text = re.sub(other_regex, "", text)

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
        if feed_response is not None:
            items = feed_response
            post_attr = "post"
            cursor_in_params = False
        elif posts_response is not None:
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
    nickname = "tRumP bot"
    hashtags = [
        "TrumpTweets",
        "TrumpTweet",
        "TheresAlwaysATweet",
        "donaldtrump",
        "trump",
    ]
    accounts = ["trumptweets.bsky.social", "trumpwatch.skyfleet.blue"]
    twix_user_full_name = "Donald J. Trump"
    twix_user_handle = "@realDonaldTrump"
    untruth_social_user_full_name = "Donald J. Trump"
    untruth_social_user_handle = "@realDonaldTrump"


class MuskTweetCompiler(TweetCompiler):
    nickname = "mUsK bot"
    hashtags = [
        "ElonMuskTweets",
        "elonmusk",
        "justnoelon",
    ]
    accounts = []
    twix_user_full_name = "Elon Musk"
    twix_user_handle = "@elonmusk"


class RowlingTweetCompiler(TweetCompiler):
    nickname = "mX. jOaN rowLinG bot"
    hashtags = [
        "JKRowlingTweets",
        "jkrowling",
        "joanrowling",
    ]
    accounts = []
    twix_user_full_name = "J.K. Rowling"
    twix_user_handle = "@jk_rowling"


TWEET_COMPILER_CLASSES = frozenset(
    [
        # MuskTweetCompiler,
        TrumpTweetCompiler,
        # RowlingTweetCompiler,
    ]
)


def dedupe_combined_tweets_list(combined_tweets_list: list[str]) -> list[str]:
    deduped_tweets_list: list[str] = []
    for tweet in combined_tweets_list:
        is_duplicate = False
        for deduped in deduped_tweets_list:
            if fuzz.ratio(tweet, deduped) >= TWEET_DEDUPE_FUZZY_MATCH_THRESHOLD:
                is_duplicate = True
                break
        if not is_duplicate:
            deduped_tweets_list.append(tweet)

    return deduped_tweets_list


def get_villain_quotes_list(max_count: int) -> list[str]:
    # The dict keys were just for reference in case anyone was curious :P
    all_quotes = [quote for quotes in VILLAIN_QUOTES.values() for quote in quotes]
    return random.sample(all_quotes, min([len(all_quotes), max_count]))


# I figured this variable would make a terrible first impression on repo
# viewers if I put it at the very top :)
MY_DUMB_INFINITE_LOOP_PREVENTER = 1000


def create_combined_corpus(
    full_tweets_list: list[str], villain_quotes_list: list[str]
) -> str:
    """
    Intersperse villain quotes at random places in tweets list then combine into
    single string corpus. Same idea as different_from_me_should repo.
    """
    num_tweets = len(full_tweets_list)
    seen_neighbor_indices = set()
    there_just_isnt_enough = 0
    for quote in villain_quotes_list:
        index_to_insert_at = random.randrange(num_tweets)
        while (
            index_to_insert_at in seen_neighbor_indices
            and there_just_isnt_enough < MY_DUMB_INFINITE_LOOP_PREVENTER
        ):
            there_just_isnt_enough += 1
            index_to_insert_at = random.randrange(num_tweets)
        # We don't want the quotes to end up too close together, so make a
        # decent range of indices around the current insert point become
        # off-limits. The negatives shouldn't do any harm here.
        seen_neighbor_indices.update(
            [
                *range(
                    index_to_insert_at - QUOTE_INSERTION_WINDOW_SIZE,
                    index_to_insert_at,
                ),
                *range(
                    index_to_insert_at,
                    index_to_insert_at + QUOTE_INSERTION_WINDOW_SIZE,
                ),
            ]
        )
        full_tweets_list.insert(index_to_insert_at, quote)

    return " ".join(full_tweets_list)


def format_tweet_compiler_nicknames() -> str:
    tweet_compiler_classes = list(TWEET_COMPILER_CLASSES)

    if len(tweet_compiler_classes) == 1:
        return tweet_compiler_classes[0].nickname
    if len(tweet_compiler_classes) == 2:
        return f"{tweet_compiler_classes[0].nickname} and {tweet_compiler_classes[1].nickname}"
    all_but_last = '{", ".join(tcc.nickname for tcc in tweet_compiler_classes[:-1])}'
    return f"{all_but_last}, and {tweet_compiler_classes[-1].nickname}"
