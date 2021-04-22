# Check all links on your website

Recursively crawls your website looking for broken links.

## Usage

```
git clone https://github.com/Delphinator/link_checker.git
cd link_checker
virtualenv --python=/usr/bin/python3 venv
source venv/bin/activate
pip install -r requirements.txt
./link_checker.py <start_url>...
```

Use `link_checker.py --help` to find out abour additional supported arguments.
