"""The text of the public legal pages: Terms, Privacy, Refunds and Contact.

Edit the wording here. Values in [[DOUBLE BRACKETS]] are filled in from the
settings (PLATFORM_NAME, LEGAL_NAME, LEGAL_ADDRESS, LEGAL_COUNTRY,
SUPPORT_EMAIL), so the pages always name the real operator.

This is a sensible starting point written for how the product really works.
It is not legal advice: have a lawyer in your country read it before you
launch, especially the parts on email law and data protection.
"""

from __future__ import annotations

import html

from app.config import settings

UPDATED = "25 October 2026"


def facts() -> dict[str, str]:
    name = settings.platform_name
    return {
        "PLATFORM": name,
        "COMPANY": settings.legal_name or name,
        "SUPPORT": settings.support_email or settings.sender_email or "the contact page",
        "ADDRESS": settings.legal_address or settings.company_postal_address or "",
        "COUNTRY": settings.legal_country or "India",
        "URL": settings.public_base_url.rstrip("/"),
        "TRIAL": str(settings.trial_hours),
        "UPDATED": UPDATED,
    }


def fill(text: str) -> str:
    for key, value in facts().items():
        text = text.replace(f"[[{key}]]", html.escape(value))
    return text


# ---------------------------------------------------------------------------
# Terms of Service
# ---------------------------------------------------------------------------

TERMS = """
<p>These Terms are an agreement between you and <b>[[COMPANY]]</b> ("we", "us") for your use of [[PLATFORM]]
(the "Service"). By creating an account or using the Service you agree to them. If you use the Service for a
company, you confirm you can bind that company.</p>

<h2 id="service">1. What the Service is</h2>
<p>[[PLATFORM]] provides "bots": software agents that search public sources for businesses that may need a
service you sell, record the evidence they found, and draft outreach emails for you to review. You choose which
bots to use, and you decide what is sent. Emails are sent from <b>your own mailbox</b>, under your name.</p>
<p>The leads and drafts are produced from public information by automated tools. We do not promise that a lead is
accurate, current, contactable or interested, and we do not promise any number of leads, replies or sales.</p>

<h2 id="accounts">2. Your account</h2>
<ul>
<li>You must be 18 or older and give true details. Confirm your email address when asked.</li>
<li>Keep your password safe. You are responsible for what happens under your account. Tell us at once if you think it was misused.</li>
<li>One person or company per account. Do not share logins or create several accounts to get extra free trials.</li>
</ul>

<h2 id="trial">3. Free trial, plans and payment</h2>
<ul>
<li>Each account gets one free trial of one bot. The length is shown on the bot (currently [[TRIAL]] hours by default). No card is needed.</li>
<li>After the trial you can buy a monthly or yearly plan for each bot. Plans are <b>prepaid and do not renew automatically</b>. When a plan ends, access to that bot ends unless you buy again. Buying early adds time on top of what you have left.</li>
<li>Prices are shown in Indian rupees (INR) or US dollars (USD) before you pay. Prices may include or exclude taxes as shown at checkout; you are responsible for any taxes that apply to you.</li>
<li>Payments are handled by Razorpay. We never see or store your card or bank details.</li>
<li>We may change prices or plans. A change never affects a plan you have already paid for.</li>
<li>Refunds are covered by our <a href="/refund">Refund Policy</a>.</li>
</ul>

<h2 id="your-emails">4. Your outreach is your responsibility</h2>
<p>You send the emails, from your mailbox, to your chosen recipients. You are the sender and you are responsible
for following the laws that apply to your messages and your recipients, including (as relevant) the US CAN-SPAM
Act, the UK Privacy and Electronic Communications Regulations and UK GDPR, the EU ePrivacy rules and GDPR,
Canada's anti-spam law, Australia's Spam Act, the UAE and Singapore rules, and India's Digital Personal Data
Protection Act. These laws differ by country. You must:</p>
<ul>
<li>Use your real name, company and a real postal address, and not mislead anyone about who you are or why you are writing.</li>
<li>Honour every unsubscribe and "do not contact" request quickly. The Service adds an unsubscribe link to each email and keeps a list of opt-outs; do not work around it.</li>
<li>Email only people it is lawful to email in their country. The Service limits sending to the countries shown in the app, but that is a safeguard, not legal advice.</li>
<li>Check each draft before approving it. Do not send claims you cannot back up.</li>
</ul>

<h2 id="acceptable-use">5. Acceptable use</h2>
<p>You must not use the Service to:</p>
<ul>
<li>send spam, phishing, scams, or anything unlawful, deceptive, harassing, hateful or sexually explicit;</li>
<li>promote illegal goods or services, or services you do not really offer;</li>
<li>contact people who asked not to be contacted, or attempt to hide who you are;</li>
<li>collect or use personal data for any purpose other than genuine, lawful business outreach;</li>
<li>attack, overload, scrape, reverse-engineer or resell the Service, or work around its limits (including usage caps, free-trial limits and country restrictions);</li>
<li>use the Service to harm our reputation or the reputation of our email, search or hosting providers.</li>
</ul>
<p>If we have reason to think you have broken this section, we may pause your agents, hold your outgoing mail, or
<b>block or close your account</b> without a refund, and we may report unlawful activity to the authorities.</p>

<h2 id="data">6. Your data and ours</h2>
<ul>
<li>You own your content: your settings, your leads, and your drafts. You give us permission to process them to run the Service. See the <a href="/privacy">Privacy Policy</a>.</li>
<li>Companies, websites and evidence collected from public sources are shared facts about businesses, not your private data, and may be used to serve other customers.</li>
<li>We own the Service, its software and its design. You get a personal, non-transferable right to use it while you have access.</li>
<li>You can delete your account whenever you like by asking us (see the <a href="/contact">Contact page</a>); we then delete your account data as described in the Privacy Policy.</li>
</ul>

<h2 id="availability">7. Availability and changes</h2>
<p>We work to keep the Service running but it depends on other services (search, email, hosting, payments) that
we do not control. It is provided "as is" and "as available", and it may be unavailable, slow, or change. Free
data sources have limits, and an agent may find fewer leads than you expect. We may add, change or remove features
and bots; if we remove a bot you have paid for, we will refund the unused part of your plan.</p>

<h2 id="liability">8. Limits of our responsibility</h2>
<p>To the extent the law allows: we give no warranty that the Service will meet your needs or be error-free; we
are not liable for lost profit, lost business, lost data, or any indirect or consequential loss; and our total
liability to you for anything arising from the Service is limited to the amount you paid us in the 3 months before
the event. Nothing in these Terms limits liability that cannot be limited by law.</p>
<p>You agree to compensate us for losses and claims that arise from your emails, your content, or your breach of
these Terms or the law.</p>

<h2 id="ending">9. Ending the agreement</h2>
<p>You may stop using the Service at any time. We may suspend or close your account if you break these Terms,
if a payment is reversed, or if the law requires. Blocking an account keeps its data while we look into it;
closing an account deletes it. Sections that by their nature should continue (payment, liability, your
responsibility for your emails) continue after the account ends.</p>

<h2 id="changes">10. Changes to these Terms</h2>
<p>We may update these Terms. When the change is important we will tell you by email or in the app. If you keep
using the Service after the date shown at the top, you accept the new Terms.</p>

<h2 id="law">11. Governing law</h2>
<p>These Terms are governed by the laws of [[COUNTRY]]. Disputes are decided by the courts of [[COUNTRY]], unless
the mandatory law of your country gives you the right to go to a court there. Please contact us first; most
problems can be fixed quickly.</p>

<h2 id="contact">12. Contact</h2>
<p>[[COMPANY]]<br>[[ADDRESS]]<br>Email: [[SUPPORT]]<br>See also the <a href="/contact">Contact page</a>.</p>
"""

# ---------------------------------------------------------------------------
# Privacy Policy
# ---------------------------------------------------------------------------

PRIVACY = """
<p>This policy explains what personal data <b>[[COMPANY]]</b> ("we") collects through [[PLATFORM]], why, who
sees it, and the choices you have. We write it plainly, and we only collect what the Service needs.</p>

<h2 id="who">1. Two kinds of people, two roles</h2>
<ul>
<li><b>You, our customer.</b> For the account data you give us, we are the data controller.</li>
<li><b>The business contacts the bots find.</b> These are people at companies (for example a named contact and a work email shown publicly). We collect them from public sources for you to contact for genuine business reasons. For those details we act on your behalf, and <b>you</b> decide whether and how to contact them and must follow the law when you do (see our <a href="/terms#your-emails">Terms</a>).</li>
</ul>

<h2 id="collect">2. What we collect</h2>
<table>
<tr><th>Data</th><th>Why</th></tr>
<tr><td>Account: name, email, company, password (stored only as a one-way hash), the time you accepted our Terms.</td><td>To run your account, sign you in and contact you about it.</td></tr>
<tr><td>Mailbox settings: sender name and address, postal address, and the SMTP and IMAP details you enter. Mailbox passwords are encrypted.</td><td>So the Service can send your approved emails from your mailbox and read replies and bounces in it.</td></tr>
<tr><td>Your work: agents, run results, leads, drafts, sent emails and replies, opt-out list.</td><td>To provide the Service to you. To find replies and bounces, the Service reads your inbox for messages answering emails it sent, and ignores other mail.</td></tr>
<tr><td>Business contacts and evidence from public sources: company names, public websites, work email addresses, phone numbers, job titles, public notices.</td><td>To build leads and quote real evidence in your drafts.</td></tr>
<tr><td>Payments: plan, amount, currency, status and Razorpay's order and payment ids. <b>Not</b> card or bank details; Razorpay collects those.</td><td>To give you access, keep accounting records and handle refunds.</td></tr>
<tr><td>Technical: sign-in time, IP address, browser, error logs, and a session cookie.</td><td>Security, preventing abuse and fixing problems.</td></tr>
<tr><td>Messages you send us (support, contact form).</td><td>To answer you.</td></tr>
</table>

<h2 id="basis">3. Why we may use it (legal bases)</h2>
<p>To perform our contract with you; to meet legal duties (tax and accounting); for our legitimate interests in
running a secure Service and preventing abuse; and, where the law requires it, with your consent (you can
withdraw it at any time). For business contacts, the legitimate interest of business-to-business outreach,
balanced against their right to say no, which every email makes easy.</p>

<h2 id="share">4. Who we share it with</h2>
<p>We do not sell personal data. We share it only with providers that help us run the Service, under their own
terms: our hosting and database providers; Razorpay (payments); search and data providers the bots query (they
receive search terms, not your account details); and email services needed to send our own account emails. We may
also disclose data where the law requires or to protect against fraud or abuse. If our business is sold, the data
may move with it under this policy.</p>
<p>Your emails go from your own mailbox through your own mail provider.</p>

<h2 id="transfers">5. Where it is processed</h2>
<p>Our providers may process data in countries other than yours, including India, the United States and Europe.
Where the law requires it, we use safeguards such as standard contractual clauses.</p>

<h2 id="keep">6. How long we keep it</h2>
<ul>
<li>Your account and work data: until you delete your account. Deleting it removes your agents, leads, emails, mailbox settings, plans and payment records from our systems.</li>
<li>Shared company facts from public sources stay, because they describe businesses, not you.</li>
<li>Payment records may also be kept by Razorpay and by us where the law requires accounting records to be retained.</li>
<li>Opt-outs: when a recipient unsubscribes, we keep their address on a suppression list so they are not contacted again.</li>
<li>Logs: kept for a short time for security and debugging.</li>
</ul>

<h2 id="rights">7. Your rights</h2>
<p>Depending on where you live (for example under GDPR, UK GDPR, India's DPDP Act or similar laws) you can ask
to see your data, correct it, delete it, restrict or object to its use, receive a copy, or withdraw consent, and
you can complain to your data protection authority. To use a right, write to <b>[[SUPPORT]]</b>; we reply within
30 days. You can delete much of your data yourself in the app, and ask us to delete the account.</p>
<p><b>If you are a person whose business details we hold</b> (for example a recipient of an email), you can
unsubscribe with the link in the email, or write to us to ask what we hold, to correct it or to remove it. We
will also pass opt-out requests to the customer who wrote to you.</p>

<h2 id="security">8. Security</h2>
<p>Passwords are hashed; mailbox passwords are encrypted; connections use HTTPS; access to data is limited by
account so one customer cannot see another's work; sign-in and email requests are rate-limited. No system is
perfectly secure, so please use a strong, unique password. If we find a breach that affects you, we will tell you
as the law requires.</p>

<h2 id="cookies">9. Cookies</h2>
<p>We use one essential cookie to keep you signed in. We do not use advertising cookies. Your browser may store
a few preferences (such as your chosen currency) on your device.</p>

<h2 id="children">10. Children</h2>
<p>The Service is for businesses and people aged 18 or over. We do not knowingly collect data from children.</p>

<h2 id="changes">11. Changes</h2>
<p>If we change this policy in an important way, we will tell you by email or in the app before it applies.</p>

<h2 id="contact">12. Contact and grievances</h2>
<p>[[COMPANY]]<br>[[ADDRESS]]<br>Privacy and grievance contact: <b>[[SUPPORT]]</b> (we acknowledge within 48 hours and resolve within 30 days).
You can also use the <a href="/contact">Contact page</a>.</p>
"""

# ---------------------------------------------------------------------------
# Refund Policy
# ---------------------------------------------------------------------------

REFUND = """
<p>We want you to be happy with [[PLATFORM]], and we want refunds to be simple and fair. Here is exactly how
they work.</p>

<h2 id="trial">1. Try it free first</h2>
<p>Every account can try one bot free before paying. We encourage you to use it: it is the best way to check the
Service suits you, and you do not need a card.</p>

<h2 id="first">2. 7-day refund on your first purchase of a bot</h2>
<p>If you buy a bot for the first time and it is not right for you, ask us within <b>7 days of payment</b> and
we will refund the full amount, provided that the account has not broken our <a href="/terms#acceptable-use">acceptable-use rules</a>
and the bot has not been used heavily (we mean sending more than 50 emails from it). Just write to
<b>[[SUPPORT]]</b> or use the <a href="/contact">Contact page</a> with your account email and the bot name.</p>

<h2 id="always">3. Refunds we always give</h2>
<ul>
<li><b>Duplicate or mistaken charges</b>, including when you were charged twice for the same order.</li>
<li><b>You paid but did not get access</b>, and we could not fix it within 3 working days.</li>
<li><b>A bot we remove or can no longer provide</b>: we refund the unused part of your plan.</li>
</ul>

<h2 id="not">4. What is not refunded</h2>
<ul>
<li>Renewals and later purchases, and plans after the 7 days, once you have used them. A plan simply ends at the end of its period; it does not renew by itself, so there is nothing to cancel.</li>
<li>Accounts blocked or closed for breaking our Terms.</li>
<li>Results you hoped for (replies, meetings, sales): we cannot promise these.</li>
<li>Email delivery problems caused by your mailbox, domain or provider.</li>
</ul>

<h2 id="how">5. How a refund is paid</h2>
<p>Refunds go back to the original payment method through Razorpay. After we approve one, it normally reaches you
in <b>5 to 10 working days</b>, depending on your bank or card. Refunds are made in the same currency you paid in.
When a refund is made, access to that plan ends.</p>

<h2 id="changes">6. Changes</h2>
<p>We may update this policy; the version in force when you paid applies to that payment.</p>

<h2 id="contact">7. Ask us</h2>
<p>[[COMPANY]], email <b>[[SUPPORT]]</b>, or the <a href="/contact">Contact page</a>. Please include the email you signed up with.</p>
"""

# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

CONTACT_INTRO = """
<p>Questions, billing, a refund, a privacy request, or a problem with a bot? Write to us and we will reply by
email, normally within one working day.</p>
<div class="cgrid">
  <div class="cbox"><b>Email</b><br>[[SUPPORT]]</div>
  <div class="cbox"><b>Company</b><br>[[COMPANY]]</div>
  <div class="cbox"><b>Address</b><br>[[ADDRESS]]</div>
</div>
"""

PAGES = {
    "terms": ("Terms of Service", TERMS),
    "privacy": ("Privacy Policy", PRIVACY),
    "refund": ("Refund Policy", REFUND),
}
