import re

_URL_RE = re.compile(
    r"https?://\S+|www\.\S+|\b\w[\w-]*\.(com|org|io|net|edu|gov|co|ai|dev|in|uk|au|ca|de|fr|it|jp|kr|mx|nl|nz|ru|sa|se|ch|tw|hk|us)\b",
    re.I,
)

if __name__ == "__main__":
    while True:
        url = input("Enter a URL: ").strip()
        if not url:
            break
        if _URL_RE.search(url):
            print("Valid URL")
        else:
            print("Invalid URL")
