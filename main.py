import os

import markovify
from atproto import Client

from utils import (
    MARKOVIFY_MAX_TRIES,
    MARKOVIFY_STATE_SIZE,
    TWEET_COMPILER_CLASSES,
    create_combined_corpus,
    dedupe_combined_tweets_list,
    format_tweet_compiler_nicknames,
    get_villain_quotes_list,
)


def main():
    bksy_username = os.environ.get("BKSY_USERNAME")
    bksy_app_password = os.environ.get("BKSY_APP_PW")
    if not bksy_username or not bksy_app_password:
        raise ValueError(
            "Bluesky login credentials not found among environment variables; cannot proceed."
        )

    bksy_client = Client()
    bksy_client.login(bksy_username, bksy_app_password)

    full_tweets_list = []
    for tcc in TWEET_COMPILER_CLASSES:
        tc = tcc(bksy_client)
        full_tweets_list.extend(tc.get_all_tweets())

    full_tweets_list = dedupe_combined_tweets_list(full_tweets_list)

    satisfied = False
    while not satisfied:
        # recreate corpus to vary the villain quotes
        villain_quotes_list = get_villain_quotes_list(max_count=len(full_tweets_list))
        corpus = create_combined_corpus(full_tweets_list, villain_quotes_list)
        markovifier = markovify.Text(corpus, state_size=MARKOVIFY_STATE_SIZE)

        sentence = markovifier.make_sentence(tries=MARKOVIFY_MAX_TRIES)
        decision = input(
            f"\nPost this quote? : {sentence}\n(Y to post / N to try again)\n"
        )
        if decision.lower() in ["y", "yes"]:
            satisfied = True

    bksy_client.send_post(f"{format_tweet_compiler_nicknames()} says: {sentence}")


if __name__ == "__main__":
    main()
