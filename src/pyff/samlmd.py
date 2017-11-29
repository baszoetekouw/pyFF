from __future__ import absolute_import, unicode_literals
from datetime import datetime
from .utils import parse_xml, check_signature, root, validate_document, xml_error, \
    schema, iso2datetime, duration2timedelta, filter_lang, url2host, trunc_str, subdomains, \
    has_tag, hash_id

from .logs import log
from .constants import config, NS, ATTRS, NF_URI, PLACEHOLDER_ICON
from lxml import etree
from lxml.builder import ElementMaker
from lxml.etree import DocumentInvalid
from itertools import chain
from copy import deepcopy
from .exceptions import *
from StringIO import StringIO


class EntitySet(object):
    def __init__(self, initial=None):
        self._e = dict()
        if initial is not None:
            for e in initial:
                self.add(e)

    def add(self, value):
        self._e[value.get('entityID')] = value

    def discard(self, value):
        entity_id = value.get('entityID')
        if entity_id in self._e:
            del self._e[entity_id]

    def __iter__(self):
        for e in self._e.values():
            yield e

    def __len__(self):
        return len(self._e.keys())

    def __contains__(self, item):
        return item.get('entityID') in self._e.keys()


def find_merge_strategy(strategy_name):
    if '.' not in strategy_name:
        strategy_name = "pyff.merge_strategies.%s" % strategy_name
    (mn, sep, fn) = strategy_name.rpartition('.')
    # log.debug("import %s from %s" % (fn,mn))
    module = None
    if '.' in mn:
        (pn, sep, modn) = mn.rpartition('.')
        module = getattr(__import__(pn, globals(), locals(), [modn], -1), modn)
    else:
        module = __import__(mn, globals(), locals(), [], -1)
    strategy = getattr(module, fn)  # we might aswell let this fail early if the strategy is wrongly named

    if strategy is None:
        raise MetadataException("Unable to find merge strategy %s" % strategy_name)

    return strategy


def parse_saml_metadata(source,
                        key=None,
                        base_url=None,
                        fail_on_error=False,
                        filter_invalid=True,
                        validate=True,
                        validation_errors=None):
    """Parse a piece of XML and return an EntitiesDescriptor element after validation.

:param source: a file-like object containing SAML metadata
:param key: a certificate (file) or a SHA1 fingerprint to use for signature verification
:param base_url: use this base url to resolve relative URLs for XInclude processing
:param fail_on_error: (default: False)
:param filter_invalid: (default True) remove invalid EntityDescriptor elements rather than raise an errror
:param validate: (default: True) set to False to turn off all XML schema validation
:param validation_errors: A dict that will be used to return validation errors to the caller
(but after xinclude processing and signature validation)
    """

    if validation_errors is None:
        validation_errors = dict()

    try:
        t = parse_xml(source, base_url=base_url)
        t.xinclude()

        expire_time_offset = metadata_expiration(t)

        t = check_signature(t, key)

        # get rid of ID as early as possible - probably not unique
        for e in iter_entities(t):
            if e.get('ID') is not None:
                del e.attrib['ID']

        t = root(t)

        if validate:
            if filter_invalid:
                t = filter_invalids_from_document(t, base_url=base_url, validation_errors=validation_errors)
            else:  # all or nothing
                try:
                    validate_document(t)
                except DocumentInvalid as ex:
                    raise MetadataException("schema validation failed: [%s] '%s': %s" %
                                            (base_url, source, xml_error(ex.error_log, m=base_url)))

        if t is not None:
            if t.tag == "{%s}EntityDescriptor" % NS['md']:
                t = entitiesdescriptor([t], base_url, copy=False)

    except Exception as ex:
        if fail_on_error:
            raise ex
        #traceback.print_exc(ex)
        log.error(ex)
        return None, None

    log.debug("returning %d valid entities" % len(list(iter_entities(t))))

    return t, expire_time_offset


class SAMLMetadataResourceParser():

    def __init__(self):
        pass

    def magic(self, content):
        return "EntitiesDescriptor" in content or "EntityDescriptor" in content

    def parse(self, resource, content):
        info = dict()
        info['Validation Errors'] = dict()
        t, expire_time_offset = parse_saml_metadata(StringIO(content.encode('utf8')),
                                                    key=resource.opts['verify'],
                                                    base_url=resource.url,
                                                    fail_on_error=resource.opts['fail_on_error'],
                                                    filter_invalid=resource.opts['filter_invalid'],
                                                    validate=resource.opts['validate'],
                                                    validation_errors=info['Validation Errors'])

        if expire_time_offset is not None:
            expire_time = datetime.now() + expire_time_offset
            resource.expire_time = expire_time
            info['Expiration Time'] = str(expire_time)

        resource.t = t
        resource.type = "application/samlmetadata+xml"

        return info


from .parse import add_parser
add_parser(SAMLMetadataResourceParser())


def metadata_expiration(t):
    relt = root(t)
    if relt.tag in ('{%s}EntityDescriptor' % NS['md'], '{%s}EntitiesDescriptor' % NS['md']):
        cache_duration = config.default_cache_duration
        valid_until = relt.get('validUntil', None)
        if valid_until is not None:
            now = datetime.utcnow()
            vu = iso2datetime(valid_until)
            now = now.replace(microsecond=0)
            vu = vu.replace(microsecond=0, tzinfo=None)
            return vu - now
        elif config.respect_cache_duration:
            cache_duration = relt.get('cacheDuration', config.default_cache_duration)
            if not cache_duration:
                cache_duration = config.default_cache_duration
            return duration2timedelta(cache_duration)

    return None


def filter_invalids_from_document(t, base_url, validation_errors):
    xsd = schema()
    for e in iter_entities(t):
        if not xsd.validate(e):
            error = xml_error(xsd.error_log, m=base_url)
            entity_id = e.get("entityID")
            log.warn('removing \'%s\': schema validation failed (%s)' % (entity_id, error))
            validation_errors[entity_id] = error
            if e.getparent() is None:
                return None
            e.getparent().remove(e)
    return t


def entitiesdescriptor(entities, name, lookup_fn=None, cache_duration=None, valid_until=None, validate=True, copy=True):
    """
:param lookup_fn: a function used to lookup entities by name
:param entities: a set of entities specifiers (lookup is used to find entities from this set)
:param name: the @Name attribute
:param cache_duration: an XML timedelta expression, eg PT1H for 1hr
:param valid_until: a relative time eg 2w 4d 1h for 2 weeks, 4 days and 1hour from now.
:param copy: set to False to avoid making a copy of all the entities in list. This may be dangerous.
:param validate: set to False to skip schema validation of the resulting EntitiesDesciptor element. This is dangerous!

Produce an EntityDescriptors set from a list of entities. Optional Name, cacheDuration and validUntil are affixed.
    """

    def _insert(ent):
        entity_id = ent.get('entityID', None)
        # log.debug("adding %s to set" % entity_id)
        if (ent is not None) and (entity_id is not None) and (entity_id not in seen):
            ent_insert = ent
            if copy:
                ent_insert = deepcopy(ent_insert)
            t.append(ent_insert)
            # log.debug("really adding %s to set" % entity_id)
            seen[entity_id] = True

    attrs = dict(Name=name, nsmap=NS)
    if cache_duration is not None:
        attrs['cacheDuration'] = cache_duration
    if valid_until is not None:
        attrs['validUntil'] = valid_until
    t = etree.Element("{%s}EntitiesDescriptor" % NS['md'], **attrs)
    nent = 0
    seen = {}  # TODO make better de-duplication
    for member in entities:
        if hasattr(member, 'tag'):
            _insert(member)
            nent += 1
        else:
            for entity in lookup_fn(member):
                _insert(entity)
                nent += 1

    log.debug("selecting %d entities before validation" % nent)

    if not nent:
        return None

    if validate:
        try:
            validate_document(t)
        except DocumentInvalid as ex:
            log.debug(xml_error(ex.error_log))
            raise MetadataException("XML schema validation failed: %s" % name)
    return t


def entities_list(t=None):
    """
        :param t: An EntitiesDescriptor or EntityDescriptor element

        Returns the list of contained EntityDescriptor elements
        """
    if t is None:
        return []
    elif root(t).tag == "{%s}EntityDescriptor" % NS['md']:
        return [root(t)]
    else:
        return iter_entities(t)


def iter_entities(t):
    if t is None:
        return []
    return t.iter('{%s}EntityDescriptor' % NS['md'])


def find_entity(t, e_id, attr='entityID'):
    for e in iter_entities(t):
        if e.get(attr) == e_id:
            return e
    return None


# semantics copied from https://github.com/lordal/md-summary/blob/master/md-summary
# many thanks to Anders Lordahl & Scotty Logan for the idea
def guess_entity_software(e):
    for elt in chain(e.findall(".//{%s}SingleSignOnService" % NS['md']),
                     e.findall(".//{%s}AssertionConsumerService" % NS['md'])):
        location = elt.get('Location')
        if location:
            if 'Shibboleth.sso' in location \
                    or 'profile/SAML2/POST/SSO' in location \
                    or 'profile/SAML2/Redirect/SSO' in location \
                    or 'profile/Shibboleth/SSO' in location:
                return 'Shibboleth'
            if location.endswith('saml2/idp/SSOService.php') or 'saml/sp/saml2-acs.php' in location:
                return 'SimpleSAMLphp'
            if location.endswith('user/authenticate'):
                return 'KalturaSSP'
            if location.endswith('adfs/ls') or location.endswith('adfs/ls/'):
                return 'ADFS'
            if '/oala/' in location or 'login.openathens.net' in location:
                return 'OpenAthens'
            if '/idp/SSO.saml2' in location or '/sp/ACS.saml2' in location \
                    or 'sso.connect.pingidentity.com' in location:
                return 'PingFederate'
            if 'idp/saml2/sso' in location:
                return 'Authentic2'
            if 'nidp/saml2/sso' in location:
                return 'Novell Access Manager'
            if 'affwebservices/public/saml2sso' in location:
                return 'CASiteMinder'
            if 'FIM/sps' in location:
                return 'IBMTivoliFIM'
            if 'sso/post' in location \
                    or 'sso/redirect' in location \
                    or 'saml2/sp/acs' in location \
                    or 'saml2/ls' in location \
                    or 'saml2/acs' in location \
                    or 'acs/redirect' in location \
                    or 'acs/post' in location \
                    or 'saml2/sp/ls/' in location:
                return 'PySAML'
            if 'engine.surfconext.nl' in location:
                return 'SURFConext'
            if 'opensso' in location:
                return 'OpenSSO'
            if 'my.salesforce.com' in location:
                return 'Salesforce'

    entity_id = e.get('entityID')
    if '/shibboleth' in entity_id:
        return 'Shibboleth'
    if entity_id.endswith('/metadata.php'):
        return 'SimpleSAMLphp'
    if '/openathens' in entity_id:
        return 'OpenAthens'

    return 'other'


def is_idp(entity):
    return has_tag(entity, "{%s}IDPSSODescriptor" % NS['md'])


def is_sp(entity):
    return has_tag(entity, "{%s}SPSSODescriptor" % NS['md'])


def is_aa(entity):
    return has_tag(entity, "{%s}AttributeAuthorityDescriptor" % NS['md'])


def _domains(entity):
    domains = [url2host(entity.get('entityID'))]
    for d in entity.iter("{%s}DomainHint" % NS['mdui']):
        if d.text not in domains:
            domains.append(d.text)
    return domains


def with_entity_attributes(entity, cb):
    def _stext(e):
        if e.text is not None:
            return e.text.strip()

    for ea in entity.iter("{%s}EntityAttributes" % NS['mdattr']):
        for a in ea.iter("{%s}Attribute" % NS['saml']):
            an = a.get('Name', None)
            if a is not None:
                values = filter(lambda x: x is not None, [_stext(v) for v in a.iter("{%s}AttributeValue" % NS['saml'])])
                cb(an, values)


def _all_domains_and_subdomains(entity):
    dlist = []
    try:
        for dn in _domains(entity):
            for sub in subdomains(dn):
                dlist.append(sub)
    except ValueError:
        pass
    return dlist


def entity_attribute_dict(entity):
    d = {}

    def _u(an, values):
        d[an] = values

    with_entity_attributes(entity, _u)

    d[ATTRS['domain']] = _all_domains_and_subdomains(entity)

    roles = d.setdefault(ATTRS['role'], [])
    if is_idp(entity):
        roles.append('idp')
        eca = ATTRS['entity-category']
        ec = d.setdefault(eca, [])
        if 'http://refeds.org/category/hide-from-discovery' not in ec:
            ec.append('http://pyff.io/category/discoverable')
    if is_sp(entity):
        roles.append('sp')
    if is_aa(entity):
        roles.append('aa')

    if ATTRS['software'] not in d:
        d[ATTRS['software']] = [guess_entity_software(entity)]

    return d


def entity_icon(e, langs=None):
    for ico in filter_lang(e.iter("{%s}Logo" % NS['mdui']), langs=langs):
        return dict(url=ico.text, width=ico.get('width'), height=ico.get('height'))


def privacy_statement_url(entity, langs):
    for url in filter_lang(entity.iter("{%s}PrivacyStatementURL" % NS['mdui']), langs=langs):
        return url.text


def entity_geoloc(entity):
    for loc in entity.iter("{%s}GeolocationHint" % NS['mdui']):
        pos = loc.text[5:].split(",")
        return dict(lat=pos[0], long=pos[1])


def entity_domains(entity):
    domains = []
    for d in entity.iter("{%s}DomainHint" % NS['mdui']):
        if d.text == '.':
            return []
        domains.append(d.text)
    if not domains:
        domains.append(url2host(entity.get('entityID')))
    return domains


def entity_extended_display(entity, langs=None):
    """Utility-method for computing a displayable string for a given entity.

    :param entity: An EntityDescriptor element
    :param langs: The list of languages to search in priority order
    """
    display = entity.get('entityID')
    info = ''

    for organizationName in filter_lang(entity.iter("{%s}OrganizationName" % NS['md']), langs=langs):
        info = display
        display = organizationName.text

    for organizationDisplayName in filter_lang(entity.iter("{%s}OrganizationDisplayName" % NS['md']), langs=langs):
        info = display
        display = organizationDisplayName.text

    for serviceName in filter_lang(entity.iter("{%s}ServiceName" % NS['md']), langs=langs):
        info = display
        display = serviceName.text

    for displayName in filter_lang(entity.iter("{%s}DisplayName" % NS['mdui']), langs=langs):
        info = display
        display = displayName.text

    for organizationUrl in filter_lang(entity.iter("{%s}OrganizationURL" % NS['md']), langs=langs):
        info = organizationUrl.text

    for description in filter_lang(entity.iter("{%s}Description" % NS['mdui']), langs=langs):
        info = description.text

    if info == entity.get('entityID'):
        info = ''

    return trunc_str(display.strip(), 40), trunc_str(info.strip(), 256)


def entity_display_name(entity, langs=None):
    """Utility-method for computing a displayable string for a given entity.

    :param entity: An EntityDescriptor element
    :param langs: The list of languages to search in priority order
    """
    for displayName in filter_lang(entity.iter("{%s}DisplayName" % NS['mdui']), langs=langs):
        return displayName.text.strip()

    for serviceName in filter_lang(entity.iter("{%s}ServiceName" % NS['md']), langs=langs):
        return serviceName.text.strip()

    for organizationDisplayName in filter_lang(entity.iter("{%s}OrganizationDisplayName" % NS['md']), langs=langs):
        return organizationDisplayName.text.strip()

    for organizationName in filter_lang(entity.iter("{%s}OrganizationName" % NS['md']), langs=langs):
        return organizationName.text.strip()

    return entity.get('entityID').strip()


def sub_domains(e):
    lst = []
    domains = entity_domains(e)
    for d in domains:
        for sub in subdomains(d):
            if sub not in lst:
                lst.append(sub)
    return lst


def entity_scopes(e):
    elt = e.findall('.//{%s}IDPSSODescriptor/{%s}Extensions/{%s}Scope' % (NS['md'], NS['md'], NS['shibmd']))
    if elt is None or len(elt) == 0:
        return None
    return [s.text for s in elt]


def discojson(e, langs=None):
    if e is None:
        return dict()

    title, descr = entity_extended_display(e)
    entity_id = e.get('entityID')

    d = dict(title=title,
             descr=descr,
             auth='saml',
             entityID=entity_id)

    eattr = entity_attribute_dict(e)
    if 'idp' in eattr[ATTRS['role']]:
        d['type'] = 'idp'
        d['hidden'] = 'true'
        if 'http://pyff.io/category/discoverable' in eattr[ATTRS['entity-category']]:
            d['hidden'] = 'false'
    elif 'sp' in eattr[ATTRS['role']]:
        d['type'] = 'sp'

    icon_info = entity_icon(e)
    if icon_info is not None:
        d['entity_icon'] = icon_info.get('url', PLACEHOLDER_ICON)
        d['icon_height'] = icon_info.get('height', 64)
        d['icon_width'] = icon_info.get('width', 64)

    scopes = entity_scopes(e)
    if scopes is not None and len(scopes) > 0:
        d['scope'] = ",".join(scopes)

    keywords = filter_lang(e.iter("{%s}Keywords" % NS['mdui']), langs=langs)
    if keywords is not None:
        lst = [elt.text for elt in keywords]
        if len(lst) > 0:
            d['keywords'] = ",".join(lst)
    psu = privacy_statement_url(e, langs)
    if psu:
        d['privacy_statement_url'] = psu
    geo = entity_geoloc(e)
    if geo:
        d['geo'] = geo

    return d

def sha1_id(e):
    return hash_id(e, 'sha1')

def entity_simple_summary(e):
    if e is None:
        return dict()

    title, descr = entity_extended_display(e)
    entity_id = e.get('entityID')
    d = dict(title=title,
             descr=descr,
             entityID=entity_id,
             domains=";".join(sub_domains(e)),
             id=hash_id(e, 'sha1'))
    icon_info = entity_icon(e)
    if icon_info is not None:
        url = icon_info.get('url', 'data:image/gif;base64,R0lGODlhAQABAIABAP///wAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==')
        d['icon_url'] = url
        d['entity_icon'] = url

    psu = privacy_statement_url(e, None)
    if psu:
        d['privacy_statement_url'] = psu

    return d


def entity_extensions(e):
    """Return a list of the Extensions elements in the EntityDescriptor

:param e: an EntityDescriptor
:return: a list
    """
    ext = e.find("./{%s}Extensions" % NS['md'])
    if ext is None:
        ext = etree.Element("{%s}Extensions" % NS['md'])
        e.insert(0, ext)
    return ext


def annotate_entity(e, category, title, message, source=None):
    """Add an ATOM annotation to an EntityDescriptor or an EntitiesDescriptor. This is a simple way to
    add non-normative text annotations to metadata, eg for the purpuse of generating reports.

:param e: An EntityDescriptor or an EntitiesDescriptor element
:param category: The ATOM category
:param title: The ATOM title
:param message: The ATOM content
:param source: An optional source URL. It is added as a <link> element with @rel='saml-metadata-source'
    """
    if e.tag != "{%s}EntityDescriptor" % NS['md'] and e.tag != "{%s}EntitiesDescriptor" % NS['md']:
        raise MetadataException('I can only annotate EntityDescriptor or EntitiesDescriptor elements')
    subject = e.get('Name', e.get('entityID', None))
    atom = ElementMaker(nsmap={'atom': 'http://www.w3.org/2005/Atom'}, namespace='http://www.w3.org/2005/Atom')
    args = [atom.published("%s" % datetime.now().isoformat()),
            atom.link(href=subject, rel="saml-metadata-subject")]
    if source is not None:
        args.append(atom.link(href=source, rel="saml-metadata-source"))
    args.extend([atom.title(title),
                 atom.category(term=category),
                 atom.content(message, type="text/plain")])
    entity_extensions(e).append(atom.entry(*args))


def _entity_attributes(e):
    ext = entity_extensions(e)
    ea = ext.find(".//{%s}EntityAttributes" % NS['mdattr'])
    if ea is None:
        ea = etree.Element("{%s}EntityAttributes" % NS['mdattr'])
        ext.append(ea)
    return ea


def _eattribute(e, attr, nf):
    ea = _entity_attributes(e)
    a = ea.xpath(".//saml:Attribute[@NameFormat='%s' and @Name='%s']" % (nf, attr),
                 namespaces=NS,
                 smart_strings=False)
    if a is None or len(a) == 0:
        a = etree.Element("{%s}Attribute" % NS['saml'])
        a.set('NameFormat', nf)
        a.set('Name', attr)
        ea.append(a)
    else:
        a = a[0]
    return a


def set_entity_attributes(e, d, nf=NF_URI):
    """Set an entity attribute on an EntityDescriptor

:param e: The EntityDescriptor element
:param d: A dict of attribute-value pairs that should be added as entity attributes
:param nf: The nameFormat (by default "urn:oasis:names:tc:SAML:2.0:attrname-format:uri") to use.
:raise: MetadataException unless e is an EntityDescriptor element
    """
    if e.tag != "{%s}EntityDescriptor" % NS['md']:
        raise MetadataException("I can only add EntityAttribute(s) to EntityDescriptor elements")

    for attr, value in d.iteritems():
        a = _eattribute(e, attr, nf)
        velt = etree.Element("{%s}AttributeValue" % NS['saml'])
        velt.text = value
        a.append(velt)


def set_pubinfo(e, publisher=None, creation_instant=None):
    if e.tag != "{%s}EntitiesDescriptor" % NS['md']:
        raise MetadataException("I can only set RegistrationAuthority to EntitiesDescriptor elements")
    if publisher is None:
        raise MetadataException("At least publisher must be provided")

    if creation_instant is None:
        now = datetime.utcnow()
        creation_instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    ext = entity_extensions(e)
    pi = ext.find(".//{%s}PublicationInfo" % NS['mdrpi'])
    if pi is not None:
        raise MetadataException("A PublicationInfo element is already present")
    pi = etree.Element("{%s}PublicationInfo" % NS['mdrpi'])
    pi.set('publisher', publisher)
    if creation_instant:
        pi.set('creationInstant', creation_instant)
    ext.append(pi)


def set_reginfo(e, policy=None, authority=None):
    if e.tag != "{%s}EntityDescriptor" % NS['md']:
        raise MetadataException("I can only set RegistrationAuthority to EntityDescriptor elements")
    if authority is None:
        raise MetadataException("At least authority must be provided")
    if policy is None:
        policy = dict()

    ext = entity_extensions(e)
    ri = ext.find(".//{%s}RegistrationInfo" % NS['mdrpi'])
    if ri is not None:
        raise MetadataException("A RegistrationInfo element is already present")

    ri = etree.Element("{%s}RegistrationInfo" % NS['mdrpi'])
    ext.append(ri)
    ri.set('registrationAuthority', authority)
    for lang, policy_url in policy.iteritems():
        rp = etree.Element("{%s}RegistrationPolicy" % NS['mdrpi'])
        rp.text = policy_url
        rp.set('{%s}lang' % NS['xml'], lang)
        ri.append(rp)


def expiration(t):
    relt = root(t)
    if relt.tag in ('{%s}EntityDescriptor' % NS['md'], '{%s}EntitiesDescriptor' % NS['md']):
        cache_duration = config.default_cache_duration
        valid_until = relt.get('validUntil', None)
        if valid_until is not None:
            now = datetime.utcnow()
            vu = iso2datetime(valid_until)
            now = now.replace(microsecond=0)
            vu = vu.replace(microsecond=0, tzinfo=None)
            return vu - now
        elif config.respect_cache_duration:
            cache_duration = relt.get('cacheDuration', config.default_cache_duration)
            return duration2timedelta(cache_duration)

    return None