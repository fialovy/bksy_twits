import easyocr
from atproto import Client
import os

# confidence threshold?
TRUMP_TWEETS = 'trumptweets.bsky.social'
HANDLES = [TRUMP_TWEETS]
MAX_PAGES = 100
# def reasonably_confident_its_their_tweet(...)  per person

def main():
    bksy_username = os.environ.get('BKSY_USERNAME')
    bksy_app_password = os.environ.get('BKSY_APP_PW')
    if not bksy_username or not bksy_app_password:
        print('Bluesky login credentials not found among environment variables; cannot proceed.')
        exit(1)

    bksy_client = Client()
    bksy_client.login(bksy_username, bksy_app_password)

    handle = TRUMP_TWEETS
    feed_data = bksy_client.get_author_feed(handle)
    pages_seen = 0
    while pages_seen <= MAX_PAGES:
        page_feed = feed_data.feed

        for item in page_feed:
            if item.post and item.post.embed and item.post.embed.images:
                print(f'...there was a post with image')
                for image in item.post.embed.images:
                    image_url = image.fullsize
            #import pdb; pdb.set_trace()
        pages_seen += 1
        print(f'seen {pages_seen} pages')
        next_page = feed_data.cursor
        if next_page:
            feed_data = bksy_client.get_author_feed(handle, cursor=next_page)
        else:
            break
    #reader = easyocr.Reader(['en'])
    # each item in output list is bounding box, text_detected_confidence_level
    #result = reader.readtext('test_tweet_images/sample_tweet.jpg')
    # example: result[0][-2]
    #print(result)

if __name__ == "__main__":
    main()
