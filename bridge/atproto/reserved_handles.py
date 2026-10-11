"""Built-in list of handle labels no Matrix user may claim.

A local user's ATProto handle is ``<label>.<bridge.domain>`` -- a real
hostname directly under the site's own domain. That makes a handle a much
more sensitive thing to hand out than a username: whoever holds
``admin.example.org`` or ``login.example.org`` holds a name that reads as
official site infrastructure, and that the operator may want for a real
service later.

This list is the "likely to matter to any site" half of the reservation
system. The other half -- subdomains THIS site actually uses -- comes from
``handles.reserved`` in config.yaml and the live routing probe (see
``bridge.atproto.handles.HandlePolicy`` and
``bridge.atproto.handle_probe``). An operator who really does want to hand
one of these out can exempt it with ``handles.allowed``.

Entries are compared against the label both as-is and with hyphens
stripped, so ``web-mail`` and ``e-mail`` are caught by ``webmail`` and
``email`` without listing every spelling here.
"""

from __future__ import annotations

_WEB_AND_HOSTING = """
www www1 www2 www3 web site home index root origin edge proxy gateway gw
cdn static assets asset media img images image files file upload uploads
download downloads dl cache app apps api apis rest graphql rpc xrpc ws
websocket socket dev devel develop staging stage test testing qa uat demo
beta alpha preview sandbox canary prod production live internal intranet
private public local localhost localdomain host hostname server servers
node nodes cluster lb loadbalancer backend frontend
"""

_MAIL = """
mail mail1 mail2 email smtp smtps imap imaps pop pop3 pop3s mx mx1 mx2 mx3
webmail mailer mta relay bounce bounces newsletter lists list listserv
autoconfig autodiscover dkim spf dmarc mailerdaemon noreply donotreply
"""

_DNS_AND_NETWORK = """
ns ns0 ns1 ns2 ns3 ns4 dns dns1 dns2 resolver ntp time whois rdap vpn
wireguard wg ssh sftp ftp ftps git svn rsync ldap ldaps radius proxy socks
tor onion i2p router firewall wpad isatap
"""

# RFC 2142 role mailboxes plus the names certificate authorities and other
# sites treat as proof of domain control, and the ones that just read as
# "the people running this place".
_ROLES_AND_AUTHORITY = """
admin administrator admins root sysadmin sysop superuser sudo system
hostmaster webmaster postmaster abuse security noc info contact help
support helpdesk service services staff team official mod mods moderator
moderators moderation owner operator ops devops trust safety legal dmca
privacy compliance press pr marketing sales billing payments pay invoice
invoices accounts accounting finance hr jobs careers ceo founder
"""

_AUTH_AND_ACCOUNT = """
auth oauth oauth2 oidc sso saml login logout signin signout signup register
registration account password passwd reset recover recovery verify
verification confirm id identity idp token tokens session sessions secure
ssl tls cert certs certificate acme pki ca keys key vault secrets
"""

_SITE_SECTIONS = """
about blog news status health healthcheck metrics stats analytics monitor
monitoring grafana prometheus kibana logs log docs doc documentation wiki
kb faq forum forums community chat talk meet call calls video voice
shop store pay donate search explore discover feed feeds rss atom
dashboard panel console portal cp cpanel manage management settings config
backup backups archive db database sql mysql postgres postgresql redis
storage s3 bucket cloud drive share paste pastebin cal calendar
"""

_MATRIX = """
matrix synapse dendrite conduit element riot cinny schildi hydrogen
federation fed homeserver hs client turn stun coturn jitsi livekit sfu
rtc call mas slidingsync sygnal push identity bridge bridges bot bots
appservice
"""

_ATPROTO_AND_FEDIVERSE = """
atproto at bsky bluesky pds plc did relay bgs appview firehose jetstream
labeler ozone feedgen handle handles activitypub ap fedi fediverse
mastodon pleroma akkoma misskey peertube lemmy nostr
"""

RESERVED_HANDLE_LABELS: frozenset[str] = frozenset(
    " ".join(
        (
            _WEB_AND_HOSTING,
            _MAIL,
            _DNS_AND_NETWORK,
            _ROLES_AND_AUTHORITY,
            _AUTH_AND_ACCOUNT,
            _SITE_SECTIONS,
            _MATRIX,
            _ATPROTO_AND_FEDIVERSE,
        )
    ).split()
)
