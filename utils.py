from abc import ABC, abstractmethod
from typing import Callable, Optional, Union

from atproto import Client
from easyocr import Reader as ImageReader


class TweetCompiler(ABC):
    max_pages: int = 100
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

    def get_tweet_text_if_confident(self, image_url: str) -> Union[str, None]:
        """
        Use what we know about person's account screenshot appearance to
        decide if we have found an image of their tweet
        maybe get fancy and return confidence float instead
        """
        extracted_texts = []
        reader_output = self.image_reader.readtext(image_url)
        for _, text, confidence_level in reader_output:
            if confidence_level >= 0.5:
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
                if item.post and item.post.embed and item.post.embed.images:
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
    accounts = ["trumptweets.bsky.social"]

    def is_probably_their_tweet(self, extracted_image_texts: list[str]) -> bool:
        import pdb

        pdb.set_trace()
        raise NotImplementedError


TWEET_COMPILER_CLASSES = [
    TrumpTweetCompiler,
]
