import datetime
import logging
import os

from google.appengine.ext import deferred
from google.appengine.ext import webapp

import config
import markup
import models
import post_deploy
import utils
import xsrfutil

#from django import forms
#from google.appengine.ext.db import djangoforms
from wtforms import Form, Field, StringField, SelectField, BooleanField, validators, ValidationError
from wtforms.widgets import TextArea, TextInput

class TagListField(Field):
    widget = TextInput()

    def _value(self):
        if self.data:
            return u', '.join(self.data)
        else:
            return u''

    def process_formdata(self, valuelist):
        if valuelist:
            self.data = set([x.strip() for x in valuelist[0].split(',')])
        else:
            self.data = []


class PostForm(Form):
  title = StringField(id='name')
  body = StringField(id='message', widget=TextArea())
  body_markup = SelectField(
    choices=[(k, v[0]) for k, v in markup.MARKUP_MAP.iteritems()])
  tags = TagListField()
  draft = BooleanField()


def with_post(fun):
  def decorate(self, post_id=None):
    post = None
    if post_id:
      post = models.BlogPost.get_by_id(int(post_id))
      if not post:
        self.error(404)
        return
    fun(self, post)
  return decorate


class BaseHandler(webapp.RequestHandler):
  def render_to_response(self, template_name, template_vals=None, theme=None):
    if not template_vals:
      template_vals = {}
    template_vals.update({
        'path': self.request.path,
        'handler_class': self.__class__.__name__,
        'is_admin': True,
    })
    template_name = os.path.join("admin", template_name)
    self.response.out.write(utils.render_template(template_name, template_vals,
                                                  theme))


class AdminHandler(BaseHandler):
  def get(self):
    offset = int(self.request.get('start', 0))
    count = int(self.request.get('count', 20))
    posts = models.BlogPost.all().order('-published').fetch(count, offset)
    template_vals = {
        'offset': offset,
        'count': count,
        'last_post': offset + len(posts) - 1,
        'prev_offset': max(0, offset - count),
        'next_offset': offset + count,
        'posts': posts,
    }
    self.render_to_response("index.html", template_vals)


class PostHandler(BaseHandler):
  def render_form(self, form):
    self.render_to_response("edit.html", {'form': form})

  @with_post
  def get(self, post):
    self.render_form(PostForm(
        obj=post,
        data={
          'draft': post and not post.path,
          'body_markup': post and post.body_markup or config.default_markup,
        }))

  @xsrfutil.xsrf_protect
  @with_post
  def post(self, post):
    form = PostForm(formdata=self.request.POST, obj=post,
                    data={'draft': post and post.published is None})
    if form.validate():
      if post is None:
          post = models.BlogPost(
              title=form.title.data,
              body=form.body.data,
              body_markup = form.body_markup.data,
              tags = form.tags.data)
      else:
          form.populate_obj(post)

      if form.data['draft']:# Draft post
        post.published = datetime.datetime.max
        post.put()
      else:
        if not post.path: # Publish post
          post.updated = post.published = datetime.datetime.now()
        else:# Edit post
          post.updated = datetime.datetime.now()
        post.publish()
      self.render_to_response("published.html", {
          'post': post,
          'draft': form.data['draft']})
    else:
      self.render_form(form)

class DeleteHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  @with_post
  def post(self, post):
    if post.path:# Published post
      post.remove()
    else:# Draft
      post.delete()
    self.render_to_response("deleted.html", None)


class PreviewHandler(BaseHandler):
  @with_post
  def get(self, post):
    # Temporary set a published date iff it's still
    # datetime.max. Django's date filter has a problem with
    # datetime.max and a "real" date looks better.
    if post.published == datetime.datetime.max:
      post.published = datetime.datetime.now()
    self.response.out.write(utils.render_template('post.html', {
        'post': post,
        'is_admin': True}))


class RegenerateHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  def post(self):
    deferred.defer(post_deploy.PostRegenerator().regenerate)
    deferred.defer(post_deploy.PageRegenerator().regenerate)
    deferred.defer(post_deploy.try_post_deploy, force=True)
    self.render_to_response("regenerating.html")

class PageForm(Form):
  path = StringField(id='path', validators=[validators.Regexp(r'(/[a-zA-Z0-9/]+)')])
  title = StringField(id='title')
  template = SelectField(choices=config.page_templates.items())
  body = StringField(id='body', widget=TextArea())

  def validate_path(self, field):
    data = self.data['path']
    existing_page = models.Page.get_by_key_name(data)
    if not data and existing_page:
      raise ValidationError("The given path already exists.")
    return data

class PageAdminHandler(BaseHandler):
  def get(self):
    offset = int(self.request.get('start', 0))
    count = int(self.request.get('count', 20))
    pages = models.Page.all().order('-updated').fetch(count, offset)
    template_vals = {
        'offset': offset,
        'count': count,
        'prev_offset': max(0, offset - count),
        'next_offset': offset + count,
        'last_page': offset + len(pages) - 1,
        'pages': pages,
    }
    self.render_to_response("indexpage.html", template_vals)


def with_page(fun):
  def decorate(self, page_key=None):
    page = None
    if page_key:
      page = models.Page.get_by_key_name(page_key)
      if not page:
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.out.write('404 :(\n' + page_key)
        #self.error(404)
        return
    fun(self, page)
  return decorate


class PageHandler(BaseHandler):
  def render_form(self, form):
    self.render_to_response("editpage.html", {'form': form})

  @with_page
  def get(self, page):
    self.render_form(PageForm(
        obj=page,
        data={
          'path': page and page.path or '/',
        }))

  @xsrfutil.xsrf_protect
  @with_page
  def post(self, page):
    form = None
    # if the path has been changed, create a new page
    if page and page.path != self.request.POST['path']:
      form = PageForm(formdata=self.request.POST, obj=None, initial={})
    else:
      form = PageForm(formdata=self.request.POST, obj=page, initial={})
    if form.validate():
      oldpath = form.data['path']
      if page:
        oldpath = page.path

      if page is None:
        page = models.Page(
            path=form.path.data,
            body=form.body.data,
            title = form.title.data,
            template = form.template.data)
      else:
        form.populate_obj(page)

      page.put()
      page.updated = datetime.datetime.now()
      page.publish()
      # path edited, remove old stuff
      if page.path != oldpath:
        oldpage = models.Page.get_by_key_name(oldpath)
        oldpage.remove()
      self.render_to_response("publishedpage.html", {'page': page})
    else:
      self.render_form(form)


class PageDeleteHandler(BaseHandler):
  @xsrfutil.xsrf_protect
  @with_page
  def post(self, page):
    page.remove()
    self.render_to_response("deletedpage.html", None)
