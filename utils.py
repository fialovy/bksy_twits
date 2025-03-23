import re
from abc import ABC, abstractmethod
from typing import Callable, Optional, TypedDict, Union

from atproto import Client
from easyocr import Reader as ImageReader

REPLIES = "replies"
RETWEETS = "retweets"
QUOTES = "quotes"
BOOKMARKS = "bookmarks"
LIKES = "likes"


class QuantifiedPostCharacteristic(TypedDict):
    regex: str
    found: bool


class TweetCompiler(ABC):
    max_pages: int = 100
    position_threshold = 5
    ocr_probability_threshold: float = 0.5
    post_characteristics_probability_threshold: float = 0.45  # r/hmm
    hashtags: list[str]
    accounts: list[str]

    def __init__(self, bksy_client: Client):
        self.client = bksy_client
        self.image_reader = ImageReader(["en"])
        if self.hashtags is None and self.accounts is None:
            raise ValueError(
                "Must specify one or both of hashtags and accounts to search."
            )

    @abstractmethod
    def is_probably_their_tweet(self, extracted_image_texts: list[str]) -> bool:
        pass

    def _within_n_positions(
        self,
        word_group: str,
        word_group_to_index: dict[str, int],
        n: Optional[int] = None,
        from_end: bool = False,  # within last n instead of within first n
    ) -> bool:
        if n is None:
            n = self.position_threshold
        if word_group not in word_group_to_index:
            return False
        if not from_end:
            return word_group_to_index[word_group] < n
        return len(word_group_to_index) - word_group_to_index[word_group] <= n

    def is_probably_their_twix_post(
        self,
        extracted_image_texts: list[str],
        platform_user_full_name: str,
        platform_user_handle: str,
        post_characteristics_probability_threshold: Optional[float] = None,
    ) -> bool:
        """
        Optional: if you know the person is on Twitter/X, call this with
        their name and handle

        Use what we know about person's account screenshot appearance to
        decide if we have found an image of one of their tweets
        Seems the bottom matter is more of just icons, so a few less checks to do

        TODO: can consolidate with untruth social? meh
        """
        if post_characteristics_probability_threshold is None:
            post_characteristics_probability_threshold = (
                self.post_characteristics_probability_threshold
            )
        twix_generics = {
            RETWEETS: QuantifiedPostCharacteristic(regex=".* Reposts?$", found=False),
            QUOTES: QuantifiedPostCharacteristic(regex=".* Quotes?$", found=False),
            LIKES: QuantifiedPostCharacteristic(regex=".* Likes?$", found=False),
            BOOKMARKS: QuantifiedPostCharacteristic(
                regex=".* Bookmarks?$", found=False
            ),
        }
        word_group_positions = {}
        for index, word_group in enumerate(extracted_image_texts):
            # stuff with numbers that needs to be normalized (in giant air quotes)
            for characteristic_name, characteristic in twix_generics.items():
                if not characteristic["found"] and re.match(
                    characteristic["regex"], word_group.strip()
                ):
                    word_group_positions[characteristic_name] = index
                    twix_generics[characteristic_name]["found"] = True
            # everything else
            if word_group not in word_group_positions:
                word_group_positions[word_group] = index
            # i really don't expect this to happen but i also don't want some later
            # text than somehow matches an expected intro item to override the index
            else:
                word_group_positions[f"{word_group}*"] = index

        probability_points = 0
        total_points = 0
        for post_characteristic, position_threshold, from_end in [
            (platform_user_full_name, self.position_threshold, False),
            (platform_user_handle, self.position_threshold + 1, False),
        ]:
            if self._within_n_positions(
                post_characteristic,
                word_group_positions,
                n=position_threshold,
                from_end=from_end,
            ):
                probability_points += 1
            total_points += 1

        return (
            probability_points / total_points
            >= post_characteristics_probability_threshold
            if total_points
            else False
        )

    def is_probably_their_untruth_social_post(
        self,
        extracted_image_texts: list[str],
        platform_user_full_name: str,
        platform_user_handle: str,
        platform_intro="Truth Details",
        post_characteristics_probability_threshold: Optional[float] = None,
    ) -> bool:
        """
        Optional: if you know the person is on Untruth Social, call this with
        their name, handle, etc.

        Use what we know about person's account screenshot appearance to
        decide if we have found an image of one of their Untruth Social posts
        """
        if post_characteristics_probability_threshold is None:
            post_characteristics_probability_threshold = (
                self.post_characteristics_probability_threshold
            )

        untruth_generics = {
            REPLIES: QuantifiedPostCharacteristic(regex=".* repl(y|ies)$", found=False),
            RETWEETS: QuantifiedPostCharacteristic(regex=".* ReTruths?$", found=False),
            LIKES: QuantifiedPostCharacteristic(regex=".* Likes?$", found=False),
        }
        word_group_positions = {}
        for index, word_group in enumerate(extracted_image_texts):
            # stuff with numbers that needs to be normalized (in giant air quotes)
            for characteristic_name, characteristic in untruth_generics.items():
                if not characteristic["found"] and re.match(
                    characteristic["regex"], word_group.strip()
                ):
                    word_group_positions[characteristic_name] = index
                    untruth_generics[characteristic_name]["found"] = True
            # everything else
            if word_group not in word_group_positions:
                word_group_positions[word_group] = index
            # i really don't expect this to happen but i also don't want some later
            # text than somehow matches an expected intro item to override the index
            else:
                word_group_positions[f"{word_group}*"] = index

        # We think it is probably an Untruth Social post if it has certain introductory
        # material within the first few indexes of the extracted text
        # as well as if it has certain footer material, but both are not required
        # because screenshots vary in crop
        probability_points = 0
        total_points = 0
        for post_characteristic, position_threshold, from_end in [
            (platform_intro, self.position_threshold, False),
            (REPLIES, self.position_threshold + 1, False),
            (platform_user_full_name, self.position_threshold + 2, False),
            (platform_user_handle, self.position_threshold + 3, False),
            # re-untruths and likes are typically near bottom of image as opposed to top
            (RETWEETS, self.position_threshold + 1, True),
            (LIKES, self.position_threshold, True),
        ]:
            if self._within_n_positions(
                post_characteristic,
                word_group_positions,
                n=position_threshold,
                from_end=from_end,
            ):
                probability_points += 1
            total_points += 1

        return (
            probability_points / total_points
            >= post_characteristics_probability_threshold
            if total_points
            else False
        )

    def get_tweet_text_if_confident(self, image_url: str) -> Union[str, None]:
        extracted_texts = []
        reader_output = self.image_reader.readtext(image_url)
        for _, text, confidence_level in reader_output:
            if confidence_level >= self.ocr_probability_threshold:
                extracted_texts.append(text)

        if self.is_probably_their_tweet(extracted_texts):
            return " ".join(extracted_texts)
        return None

    def get_tweets_list_from_hashtag(self) -> list[str]:
        raise NotImplementedError

    def get_tweets_list_from_account(self, account) -> list[str]:
        tweets_list = []
        pages_seen = 0
        feed_data = self.client.get_author_feed(account)
        while pages_seen <= self.max_pages:
            page_feed = feed_data.feed

            for item in page_feed:
                if item.post and item.post.embed and hasattr(item.post.embed, 'images'):
                    for image in item.post.embed.images:
                        tweet_text = self.get_tweet_text_if_confident(image.fullsize)
                        if tweet_text is not None:
                            # TODO: clean tweet text somewhere to remove intro stuff
                            tweets_list.append(tweet_text)

            pages_seen += 1
            next_page = feed_data.cursor
            if next_page:
                feed_data = self.client.get_author_feed(account, cursor=next_page)
            else:
                break

        return tweets_list

    def get_all_tweets(self) -> list[str]:
        all_tweets = []
        # for hashtag in self.hashtags:
        #    all_tweets.extend(self.get_tweets_list_from_hashtag(hashtag))
        for account in self.accounts:
            all_tweets.extend(self.get_tweets_list_from_account(account))
        return all_tweets


class TrumpTweetCompiler(TweetCompiler):
    hashtags = ["TrumpTweets"]
    accounts = ["trumptweets.bsky.social", "trumpwatch.skyfleet.blue"]

    def is_probably_their_tweet(self, extracted_image_texts: list[str]) -> bool:
        return self.is_probably_their_untruth_social_post(
            extracted_image_texts,
            platform_user_full_name="Donald J. Trump",
            platform_user_handle="@realDonaldTrump",
        ) or self.is_probably_their_twix_post(
            extracted_image_texts,
            platform_user_full_name="Donald J. Trump",
            platform_user_handle="@realDonaldTrump",
        )


TWEET_COMPILER_CLASSES = [
    TrumpTweetCompiler,
]
