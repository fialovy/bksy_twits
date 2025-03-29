import os

import markovify
from atproto import Client

from utils import (
    MARKOVIFY_MAX_TRIES,
    MARKOVIFY_STATE_SIZE,
    TWEET_COMPILER_CLASSES,
    create_combined_corpus,
    get_villain_quotes_list,
)


def main():
    bksy_username = os.environ.get("BKSY_USERNAME")
    bksy_app_password = os.environ.get("BKSY_APP_PW")
    if not bksy_username or not bksy_app_password:
        print(
            "Bluesky login credentials not found among environment variables; cannot proceed."
        )
        exit(1)

    bksy_client = Client()
    bksy_client.login(bksy_username, bksy_app_password)

    full_tweets_list = []
    for tcc in TWEET_COMPILER_CLASSES:
        tc = tcc(bksy_client)
        full_tweets_list.extend(tc.get_all_tweets())

    # TODO: possibly filter similar list items that are really just the same screenshot
    # Buuuut, I believe markovify ignores repeated stuff anyway, so ¯\_(ツ)_/¯
    villain_quotes_list = get_villain_quotes_list()
    corpus = create_combined_corpus(full_tweets_list, villain_quotes_list)

    markovifier = markovify.Text(corpus, state_size=MARKOVIFY_STATE_SIZE)
    satisfied = False
    while not satisfied:
        sentence = markovifier.make_sentence(tries=MARKOVIFY_MAX_TRIES)
        decision = input(f"Post this quote? : {sentence} (Y to post / N to try again)")
        if decision.lower() in ["y", "yes"]:
            satisfied = True

    # POST IT:


if __name__ == "__main__":
    main()
