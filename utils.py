import re
from abc import ABC, abstractmethod
from typing import Callable, NamedTuple, Optional, TypedDict, Union

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


class ExpectedPostCharacteristicInfo(NamedTuple):
    post_characteristic: str
    position_threshold: int
    from_end: bool


class TweetCompiler(ABC):
    max_pages: int = 100
    # Within how many positions in the list of extracted image texts we expect to
    # see a desired piece of text (e.g., person's handle or like count)
    position_threshold = 5
    # threshold to pass to the image processing library
    ocr_probability_threshold: float = 0.5
    # if we find at least this proportion of the expected pieces of a screeshot's
    # appearance, assume we found a post
    post_characteristics_probability_threshold: float = 0.4

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

    @abstractmethod
    def get_untruth_social_generics(self) -> dict[str, QuantifiedPostCharacteristic]:
        """
        Return empty dict if not on untruth social
        Yes i will be modifying this in some loop. And yes i am ashamed. 🤪
        """
        pass

    @abstractmethod
    def get_twix_generics(self) -> dict[str, QuantifiedPostCharacteristic]:
        """
        Return empty dict if not on twitter/x
        """
        pass

    @abstractmethod
    def is_probably_their_tweet(self, extracted_image_texts: list[str]) -> bool:
        # e.g., call one or both of is_probably_their_twix_post or
        # is_probably_their_untruth_social_post depending on if the user
        # has a twitter/X accound or Untruth Social account, respectively
        pass

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
        platform_generics: dict[str, QuantifiedPostCharacteristic],
        expected_image_position_infos: list[ExpectedPostCharacteristicInfo],
        probability_threshold: Optional[float] = None,
    ) -> bool:
        if probability_threshold is None:
            probability_threshold = self.post_characteristics_probability_threshold

        word_group_positions = {}
        for index, word_group in enumerate(extracted_image_texts):
            # stuff with numbers that needs to be normalized (in giant air quotes)
            for characteristic_name, characteristic in platform_generics.items():
                if not characteristic["found"] and re.match(
                    characteristic["regex"], word_group.strip()
                ):
                    word_group_positions[characteristic_name] = index
                    platform_generics[characteristic_name]["found"] = True
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
        for (
            post_characteristic,
            position_threshold,
            from_end,
        ) in expected_image_position_infos:
            if self._within_n_positions(
                post_characteristic,
                word_group_positions,
                n=position_threshold,
                from_end=from_end,
            ):
                probability_points += 1
            total_points += 1

        return (
            probability_points / total_points >= probability_threshold
            if total_points
            else False
        )

    def is_probably_their_twix_post(
        self,
        extracted_image_texts: list[str],
        platform_user_full_name: str,
        platform_user_handle: str,
    ) -> bool:
        """
        Optional: if you know the person is on Twitter/X, call this with
        their name and handle

        Use what we know about person's account screenshot appearance to
        decide if we have found an image of one of their tweets
        """
        twix_expected_image_position_infos = [
            ExpectedPostCharacteristicInfo(
                post_characteristic=platform_user_full_name,
                position_threshold=self.position_threshold,
                from_end=False,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=platform_user_handle,
                position_threshold=self.position_threshold + 1,
                from_end=False,
            ),
            # Reposts, quotes, likes, and/or bookmarks seem to appear at bottom
            # of screenshots if indeed they made it into the crop
            # DOUBLE CHECK THIS PROBABLY - are they actually separate list items??
            ExpectedPostCharacteristicInfo(
                post_characteristic=RETWEETS,
                position_threshold=self.position_threshold + 3,
                from_end=True,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=QUOTES,
                position_threshold=self.position_threshold + 2,
                from_end=True,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=LIKES,
                position_threshold=self.position_threshold + 1,
                from_end=True,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=BOOKMARKS,
                position_threshold=self.position_threshold,
                from_end=True,
            ),
        ]
        return self.is_probably_their_platform_post(
            extracted_image_texts,
            self.twix_generics,
            twix_expected_image_position_infos,
        )

    def is_probably_their_untruth_social_post(
        self,
        extracted_image_texts: list[str],
        platform_user_full_name: str,
        platform_user_handle: str,
    ) -> bool:
        """
        Optional: if you know the person is on Untruth Social, call this with
        their name, handle, etc.

        Use what we know about person's account screenshot appearance to
        decide if we have found an image of one of their Untruth Social posts
        """
        untruth_expected_image_position_infos = [
            ExpectedPostCharacteristicInfo(
                post_characteristic="Truth Details",
                position_threshold=self.position_threshold,
                from_end=False,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=REPLIES,
                position_threshold=self.position_threshold + 1,
                from_end=False,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=platform_user_full_name,
                position_threshold=self.position_threshold + 2,
                from_end=False,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=platform_user_handle,
                position_threshold=self.position_threshold + 3,
                from_end=False,
            ),
            # re-untruths and likes are typically near bottom of image as opposed to top
            ExpectedPostCharacteristicInfo(
                post_characteristic=RETWEETS,
                position_threshold=self.position_threshold + 1,
                from_end=True,
            ),
            ExpectedPostCharacteristicInfo(
                post_characteristic=LIKES,
                position_threshold=self.position_threshold,
                from_end=True,
            ),
        ]
        return self.is_probably_their_platform_post(
            extracted_image_texts,
            self.untruth_social_generics,
            untruth_expected_image_position_infos,
        )

    def clean_extracted_texts(self, extracted_texts: list[str]) -> list[str]:
        import pdb; pdb.set_trace() 

    def get_tweet_text_if_confident(self, image_url: str) -> Union[str, None]:
        extracted_texts = []
        reader_output = self.image_reader.readtext(image_url)
        for _, text, confidence_level in reader_output:
            if confidence_level >= self.ocr_probability_threshold:
                extracted_texts.append(text)

        if self.is_probably_their_tweet(extracted_texts):
            cleaned_extracted_texts = self.clean_extracted_texts(extracted_texts)
            # OR DO CLEANING HERE
            return " ".join(cleaned_extracted_texts)
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
                if item.post and item.post.embed and hasattr(item.post.embed, "images"):
                    for image in item.post.embed.images:
                        tweet_text = self.get_tweet_text_if_confident(image.fullsize)
                        if tweet_text is not None:
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
    twix_user_full_name = "Donald J. Trump"
    twix_user_handle = "@realDonaldTrump"
    untruth_social_user_full_name = "Donald J. Trump"
    untruth_social_user_handle = "@realDonaldTrump"

    def get_untruth_social_generics(self) -> dict[str, QuantifiedPostCharacteristic]:
        return {
            REPLIES: QuantifiedPostCharacteristic(regex=".* repl(y|ies)$", found=False),
            RETWEETS: QuantifiedPostCharacteristic(regex=".* ReTruths?$", found=False),
            LIKES: QuantifiedPostCharacteristic(regex=".* Likes?$", found=False),
        }
    
    def get_twix_generics(self) -> dict[str, QuantifiedPostCharacteristic]:
        return {
            RETWEETS: QuantifiedPostCharacteristic(regex=".* Reposts?$", found=False),
            QUOTES: QuantifiedPostCharacteristic(regex=".* Quotes?$", found=False),
            LIKES: QuantifiedPostCharacteristic(regex=".* Likes?$", found=False),
            BOOKMARKS: QuantifiedPostCharacteristic(
                regex=".* Bookmarks?$", found=False
            ),
        }

    def is_probably_their_tweet(self, extracted_image_texts: list[str]) -> bool:
        return self.is_probably_their_untruth_social_post(
            extracted_image_texts,
            platform_user_full_name=self.untruth_social_user_full_name,
            platform_user_handle=self.untruth_social_user_handle,
        ) or self.is_probably_their_twix_post(
            extracted_image_texts,
            platform_user_full_name=self.twix_user_full_name,
            platform_user_handle=self.twix_user_handle,
        )


TWEET_COMPILER_CLASSES = [
    TrumpTweetCompiler,
]
