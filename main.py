import os

from atproto import Client

from utils import TWEET_COMPILER_CLASSES


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

    import pdb; pdb.set_trace()


if __name__ == "__main__":
    main()
