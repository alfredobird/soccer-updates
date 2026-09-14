# Soccer updates

Checks ystpr.com twice a day for Surf Guaynabo U10 2016 Black's next match and
league position, and emails a summary when either one changes.

Runs on GitHub Actions. No cost, no server, no card.

## Setup

Add three repo secrets under Settings > Secrets and variables > Actions:

| Secret | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Google app password, not your normal password |
| `EMAIL_TO` | recipients, comma-separated |

The app password comes from myaccount.google.com/apppasswords. It only appears
if 2-Step Verification is already on for that account.

Then: Actions tab > soccer-updates > Run workflow, with the force option on.
After that it runs itself at 7am and 7pm AST.

## Cost

GitHub's free tier includes 2,000 Actions minutes a month on private repos.
This uses roughly 300.

## Tuning

Edit the top of `main.py`: `TEAM`, `TOURNAMENT`, `DIVISION`, `GROUP`, `NICK`,
`LANG_OUT` (`es` or `en`). Add `SMTP_HOST` and `SMTP_PORT` secrets if you use
something other than Gmail.

## If the email content looks wrong

Open the run on the Actions tab and download the `debug` artifact at the bottom.
It has a screenshot and a text dump of what the browser actually saw.
