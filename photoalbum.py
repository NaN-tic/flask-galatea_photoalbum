from flask import Blueprint, render_template, current_app, abort, g, \
    request, url_for, session, flash, redirect
from galatea.tryton import tryton
from galatea.utils import get_tryton_language
from flask.ext.paginate import Pagination
from flask.ext.babel import gettext as _, lazy_gettext
from flask.ext.mail import Mail, Message
from flask.ext.wtf import Form
from trytond.config import config as tryton_config
from wtforms import TextField, TextAreaField, FileField, validators
from whoosh import index
from whoosh.qparser import MultifieldParser
from mimetypes import guess_type
from slug import slug
import os

photoalbum = Blueprint('photoalbum', __name__, template_folder='templates')

DISPLAY_MSG = lazy_gettext('Displaying <b>{start} - {end}</b> of <b>{total}</b>')

Website = tryton.pool.get('galatea.website')
ConfigPhotoAlbum = tryton.pool.get('galatea.photoalbum.configuration')
PhotoalbumPhoto = tryton.pool.get('galatea.photoalbum.photo')
Comment = tryton.pool.get('galatea.photoalbum.comment')
User = tryton.pool.get('galatea.user')

GALATEA_WEBSITE = current_app.config.get('TRYTON_GALATEA_SITE')
LIMIT = current_app.config.get('TRYTON_PAGINATION_PHOTOALBUM_LIMIT', 20)
COMMENTS = current_app.config.get('TRYTON_PHOTOALBUM_COMMENTS', True)
WHOOSH_MAX_LIMIT = current_app.config.get('WHOOSH_MAX_LIMIT', 500)

PHOTOALBUM_SCHEMA_PARSE_FIELDS = ['title', 'content']
IMAGE_TYPES = ['image/jpeg', 'image/png',  'image/gif']

def _visibility():
    visibility = ['public']
    if session.get('logged_in'):
        visibility.append('register')
    if session.get('manager'):
        visibility.append('manager')
    return visibility

def is_image(message=_('Select an image file')):
    def _is_image(form, field):
        file_mime, _ = guess_type(field.data.filename)
        if not file_mime or file_mime not in IMAGE_TYPES:
            raise validators.ValidationError(message)
    return _is_image


class PhotoForm(Form):
    "Photo form"
    photo = FileField(_('Image'), [
        validators.Required(), is_image()])
    description = TextAreaField(_('Description'))
    keywords = TextField(_('Keys'), description=(_('Separated by comma ","')))

    def __init__(self, *args, **kwargs):
        Form.__init__(self, *args, **kwargs)

    def validate(self):
        rv = Form.validate(self)
        if not rv:
            return False
        return True


@photoalbum.route("/search/", methods=["GET"], endpoint="search")
@tryton.transaction()
def search(lang):
    '''Search'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    WHOOSH_PHOTO_DIR = current_app.config.get('WHOOSH_PHOTO_DIR')
    if not WHOOSH_PHOTO_DIR:
        abort(404)

    db_name = current_app.config.get('TRYTON_DATABASE')
    locale = get_tryton_language(lang)

    schema_dir = os.path.join(tryton_config.get('database', 'path'),
        db_name, 'whoosh', WHOOSH_PHOTO_DIR, locale.lower())

    if not os.path.exists(schema_dir):
        abort(404)

    #breadcumbs
    breadcrumbs = [{
        'slug': url_for('.photos', lang=g.language),
        'name': _('Photo Album'),
        }, {
        'slug': url_for('.search', lang=g.language),
        'name': _('Search'),
        }]

    q = request.args.get('q')
    if not q:
        return render_template('photoalbum-search.html',
                photos=[],
                breadcrumbs=breadcrumbs,
                pagination=None,
                q=None,
                )

    # Get photos from schema results
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1

    # Search
    ix = index.open_dir(schema_dir)
    query = q.replace('+', ' AND ').replace('-', ' NOT ')
    query = MultifieldParser(PHOTOALBUM_SCHEMA_PARSE_FIELDS, ix.schema).parse(query)

    with ix.searcher() as s:
        all_results = s.search_page(query, 1, pagelen=WHOOSH_MAX_LIMIT)
        total = all_results.scored_length()
        results = s.search_page(query, page, pagelen=LIMIT) # by pagination
        res = [result.get('id') for result in results]

    domain = [
        ('id', 'in', res),
        ('active', '=', True),
        ('visibility', 'in', _visibility()),
        ]
    order = [('photo_create_date', 'DESC'), ('id', 'DESC')]

    photos = PhotoalbumPhoto.search(domain, order=order)

    pagination = Pagination(page=page, total=total, per_page=LIMIT, display_msg=DISPLAY_MSG, bs_version='3')

    return render_template('photoalbum-search.html',
            website=website,
            photos=photos,
            pagination=pagination,
            breadcrumbs=breadcrumbs,
            q=q,
            )

@photoalbum.route("/new", methods=["GET", "POST"], endpoint="new")
@tryton.transaction()
def new(lang):
    '''New Photo Comment'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    if not website.photoalbum_new:
        flash(_('Not available to new images.'), 'danger')
        return redirect(url_for('.photos', lang=g.language))
    elif not website.photoalbum_new_anonymous and not session.get('user'):
        flash(_('Not available to publish new images and anonymous users.' \
            ' Please, login in'), 'danger')
        return redirect(url_for('galatea.login', lang=g.language))

    form = PhotoForm()
    if form.validate_on_submit():

        config_photoalbum = ConfigPhotoAlbum(1)
        size = config_photoalbum.max_size or 1000000
        photo_data = form.photo.data.read()
        if len(photo_data) > size:
            flash(_('Image size is larger than %s MB.' % str(size/1000000)), 'danger')
            return redirect(url_for('.new', lang=g.language))

        p = PhotoalbumPhoto()
        p.photo = photo_data
        try:
            file_name, __ = guess_type(form.photo.data.filename)
            ftype, extension = file_name.split('/')
            p.file_name = '%s.%s' % (
                slug(form.photo.data.filename[:-len(extension)-1]).lower(),
                extension,
                )
        except:
            p.file_name = 'unknown.jpg'

        p.user = session['user'] if session.get('user') \
            else website.photoalbum_anonymous_user.id
        p.description = form.description.data or None
        p.metakeywords = form.keywords.data or None
        p.save()
        flash(_('Image published successfully.'), 'success')

        mail = Mail(current_app)

        mail_to = current_app.config.get('DEFAULT_MAIL_SENDER')
        subject =  '%s - %s' % (current_app.config.get('TITLE'), _('New image published'))
        msg = Message(subject,
                body = render_template('emails/photoalbum-new-text.jinja', photo=p),
                html = render_template('emails/photoalbum-new-html.jinja', photo=p),
                sender = mail_to,
                recipients = [mail_to])
        mail.send(msg)

        return redirect(url_for('.photo', lang=g.language, id=p.id))

    breadcrumbs = [{
        'slug': url_for('.photos', lang=g.language),
        'name': _('Photo Album'),
        }, {
        'slug': url_for('.new', lang=g.language),
        'name': _('New'),
        }]

    return render_template('photoalbum-new.html',
            website=website,
            breadcrumbs=breadcrumbs,
            form=form,
            )

@photoalbum.route("/comment", methods=["POST"], endpoint="comment")
@tryton.transaction()
def comment(lang):
    '''Add Comment'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    photo = request.form.get('photo')
    comment = request.form.get('comment')

    domain = [
        ('id', '=', photo),
        ('active', '=', True),
        ('visibility', 'in', _visibility()),
        ('websites', 'in', [GALATEA_WEBSITE]),
        ]
    photos = PhotoalbumPhoto.search(domain, limit=1)
    if not photos:
        abort(404)
    photo, = photos

    if not website.photoalbum_comment:
        flash(_('Not available to publish comments.'), 'danger')
    elif not website.photoalbum_anonymous and not session.get('user'):
        flash(_('Not available to publish comments and anonymous users.' \
            ' Please, login in'), 'danger')
    elif not comment or not photo:
        flash(_('Add a comment to publish.'), 'danger')
    else:
        c = Comment()
        c.photo = photo['id']
        c.user = session['user'] if session.get('user') \
            else website.photoalbum_anonymous_user.id
        c.description = comment
        c.save()
        flash(_('Comment published successfully.'), 'success')

        mail = Mail(current_app)

        mail_to = current_app.config.get('DEFAULT_MAIL_SENDER')
        subject =  '%s - %s' % (current_app.config.get('TITLE'), _('New comment published'))
        msg = Message(subject,
                body = render_template('emails/photoalbum-comment-text.jinja', photo=photo, comment=comment),
                html = render_template('emails/photoalbum-comment-html.jinja', photo=photo, comment=comment),
                sender = mail_to,
                recipients = [mail_to])
        mail.send(msg)

    return redirect(url_for('.photo', lang=g.language, id=photo['id']))

@photoalbum.route("/<id>", endpoint="photo")
@tryton.transaction()
def photo(lang, id):
    '''Photo Album Photo detail'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    photos = PhotoalbumPhoto.search([
        ('id', '=', id),
        ('active', '=', True),
        ('visibility', 'in', _visibility()),
        ('websites', 'in', [GALATEA_WEBSITE]),
        ], limit=1)

    if not photos:
        abort(404)
    photo, = photos

    breadcrumbs = [{
        'slug': url_for('.photos', lang=g.language),
        'name': _('Photo Album'),
        }, {
        'slug': url_for('.user', lang=g.language, user=photo.user.id),
        'name': photo.user.rec_name,
        }]

    return render_template('photoalbum-photo.html',
            website=website,
            photo=photo,
            breadcrumbs=breadcrumbs,
            )

@photoalbum.route("/key/<key>", endpoint="key")
@tryton.transaction()
def key(lang, key):
    '''Photo Album Photos by Key'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1

    domain = [
        ('metakeywords', 'ilike', '%'+key+'%'),
        ('active', '=', True),
        ('visibility', 'in', _visibility()),
        ('websites', 'in', [GALATEA_WEBSITE]),
        ]
    total = PhotoalbumPhoto.search_count(domain)
    offset = (page-1)*LIMIT

    order = [('photo_create_date', 'DESC'), ('id', 'DESC')]
    photos = PhotoalbumPhoto.search(domain, offset, LIMIT, order)

    pagination = Pagination(page=page, total=total, per_page=LIMIT, display_msg=DISPLAY_MSG, bs_version='3')

    #breadcumbs
    breadcrumbs = [{
        'slug': url_for('.photos', lang=g.language),
        'name': _('Photo Album'),
        }, {
        'slug': url_for('.key', lang=g.language, key=key),
        'name': key,
        }]

    return render_template('photoalbum-key.html',
            website=website,
            photos=photos,
            pagination=pagination,
            breadcrumbs=breadcrumbs,
            key=key,
            )

@photoalbum.route("/user/<user>", endpoint="user")
@tryton.transaction()
def users(lang, user):
    '''Photo Album Photos by User'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    try:
        user = int(user)
    except:
        abort(404)

    users = User.search([
        ('id', '=', user)
        ], limit=1)
    if not users:
        abort(404)
    user, = users

    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1

    domain = [
        ('user', '=', user.id),
        ('active', '=', True),
        ('visibility', 'in', _visibility()),
        ('websites', 'in', [GALATEA_WEBSITE]),
        ]
    total = PhotoalbumPhoto.search_count(domain)
    offset = (page-1)*LIMIT

    if not total:
        abort(404)

    order = [('photo_create_date', 'DESC'), ('id', 'DESC')]
    photos = PhotoalbumPhoto.search(domain, offset, LIMIT, order)

    pagination = Pagination(page=page, total=total, per_page=LIMIT, display_msg=DISPLAY_MSG, bs_version='3')

    #breadcumbs
    breadcrumbs = [{
        'slug': url_for('.photos', lang=g.language),
        'name': _('Photo Album'),
        }, {
        'slug': url_for('.user', lang=g.language, user=user.id),
        'name': user.rec_name,
        }]

    return render_template('photoalbum-user.html',
            website=website,
            photos=photos,
            user=user,
            pagination=pagination,
            breadcrumbs=breadcrumbs,
            )

@photoalbum.route("/", endpoint="photos")
@tryton.transaction()
def photos(lang):
    '''Photo Album Photos'''
    websites = Website.search([
        ('id', '=', GALATEA_WEBSITE),
        ], limit=1)
    if not websites:
        abort(404)
    website, = websites

    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1

    domain = [
        ('active', '=', True),
        ('visibility', 'in', _visibility()),
        ('websites', 'in', [GALATEA_WEBSITE]),
        ]
    total = PhotoalbumPhoto.search_count(domain)
    offset = (page-1)*LIMIT

    order = [('photo_create_date', 'DESC'), ('id', 'DESC')]
    photos = PhotoalbumPhoto.search(domain, offset, LIMIT, order)

    pagination = Pagination(page=page, total=total, per_page=LIMIT, display_msg=DISPLAY_MSG, bs_version='3')

    #breadcumbs
    breadcrumbs = [{
        'slug': url_for('.photos', lang=g.language),
        'name': _('Photo Album'),
        }]

    return render_template('photoalbums.html',
            website=website,
            photos=photos,
            pagination=pagination,
            breadcrumbs=breadcrumbs,
            )
