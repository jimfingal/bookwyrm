''' models for storing different kinds of Activities '''
from dataclasses import MISSING
import re

from django.apps import apps
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from model_utils.managers import InheritanceManager

from bookwyrm import activitypub
from .activitypub_mixin import ActivitypubMixin, ActivityMixin
from .activitypub_mixin import OrderedCollectionPageMixin
from .base_model import BookWyrmModel
from .fields import image_serializer
from . import fields


class Status(OrderedCollectionPageMixin, BookWyrmModel):
    ''' any post, like a reply to a review, etc '''
    user = fields.ForeignKey(
        'User', on_delete=models.PROTECT, activitypub_field='attributedTo')
    content = fields.HtmlField(blank=True, null=True)
    mention_users = fields.TagField('User', related_name='mention_user')
    mention_books = fields.TagField('Edition', related_name='mention_book')
    local = models.BooleanField(default=True)
    content_warning = fields.CharField(
        max_length=500, blank=True, null=True, activitypub_field='summary')
    privacy = fields.PrivacyField(max_length=255)
    sensitive = fields.BooleanField(default=False)
    # created date is different than publish date because of federated posts
    published_date = fields.DateTimeField(
        default=timezone.now, activitypub_field='published')
    deleted = models.BooleanField(default=False)
    deleted_date = models.DateTimeField(blank=True, null=True)
    favorites = models.ManyToManyField(
        'User',
        symmetrical=False,
        through='Favorite',
        through_fields=('status', 'user'),
        related_name='user_favorites'
    )
    reply_parent = fields.ForeignKey(
        'self',
        null=True,
        on_delete=models.PROTECT,
        activitypub_field='inReplyTo',
    )
    objects = InheritanceManager()

    activity_serializer = activitypub.Note
    serialize_reverse_fields = [('attachments', 'attachment', 'id')]
    deserialize_reverse_fields = [('attachments', 'attachment')]


    def save(self, *args, **kwargs):
        ''' save and notify '''
        super().save(*args, **kwargs)

        notification_model = apps.get_model(
            'bookwyrm.Notification', require_ready=True)

        if self.deleted:
            notification_model.objects.filter(related_status=self).delete()

        if self.reply_parent and self.reply_parent.user != self.user and \
                self.reply_parent.user.local:
            notification_model.objects.create(
                user=self.reply_parent.user,
                notification_type='REPLY',
                related_user=self.user,
                related_status=self,
            )
        for mention_user in self.mention_users.all():
            # avoid double-notifying about this status
            if not mention_user.local or \
                    (self.reply_parent and \
                     mention_user == self.reply_parent.user):
                continue
            notification_model.objects.create(
                user=mention_user,
                notification_type='MENTION',
                related_user=self.user,
                related_status=self,
            )

    @property
    def recipients(self):
        ''' tagged users who definitely need to get this status in broadcast '''
        mentions = [u for u in self.mention_users.all() if not u.local]
        if hasattr(self, 'reply_parent') and self.reply_parent \
                and not self.reply_parent.user.local:
            mentions.append(self.reply_parent.user)
        return list(set(mentions))

    @classmethod
    def ignore_activity(cls, activity):
        ''' keep notes if they are replies to existing statuses '''
        if activity.type != 'Note':
            return False
        if cls.objects.filter(
                remote_id=activity.inReplyTo).exists():
            return False

        # keep notes if they mention local users
        if activity.tag == MISSING or activity.tag is None:
            return True
        tags = [l['href'] for l in activity.tag if l['type'] == 'Mention']
        for tag in tags:
            user_model = apps.get_model('bookwyrm.User', require_ready=True)
            if user_model.objects.filter(
                    remote_id=tag, local=True).exists():
                # we found a mention of a known use boost
                return False
        return True

    @classmethod
    def replies(cls, status):
        ''' load all replies to a status. idk if there's a better way
            to write this so it's just a property '''
        return cls.objects.filter(
            reply_parent=status
        ).select_subclasses().order_by('published_date')

    @property
    def status_type(self):
        ''' expose the type of status for the ui using activity type '''
        return self.activity_serializer.__name__

    @property
    def boostable(self):
        ''' you can't boost dms '''
        return self.privacy in ['unlisted', 'public']

    def to_replies(self, **kwargs):
        ''' helper function for loading AP serialized replies to a status '''
        return self.to_ordered_collection(
            self.replies(self),
            remote_id='%s/replies' % self.remote_id,
            collection_only=True,
            **kwargs
        )

    def to_activity(self, pure=False):# pylint: disable=arguments-differ
        ''' return tombstone if the status is deleted '''
        if self.deleted:
            return activitypub.Tombstone(
                id=self.remote_id,
                url=self.remote_id,
                deleted=self.deleted_date.isoformat(),
                published=self.deleted_date.isoformat()
            ).serialize()
        activity = ActivitypubMixin.to_activity(self)
        activity['replies'] = self.to_replies()

        # "pure" serialization for non-bookwyrm instances
        if pure and hasattr(self, 'pure_content'):
            activity['content'] = self.pure_content
            if 'name' in activity:
                activity['name'] = self.pure_name
            activity['type'] = self.pure_type
            activity['attachment'] = [
                image_serializer(b.cover, b.alt_text) \
                    for b in self.mention_books.all()[:4] if b.cover]
            if hasattr(self, 'book') and self.book.cover:
                activity['attachment'].append(
                    image_serializer(self.book.cover, self.book.alt_text)
                )
        return activity


class GeneratedNote(Status):
    ''' these are app-generated messages about user activity '''
    @property
    def pure_content(self):
        ''' indicate the book in question for mastodon (or w/e) users '''
        message = self.content
        books = ', '.join(
            '<a href="%s">"%s"</a>' % (book.remote_id, book.title) \
            for book in self.mention_books.all()
        )
        return '%s %s %s' % (self.user.display_name, message, books)

    activity_serializer = activitypub.GeneratedNote
    pure_type = 'Note'


class Comment(Status):
    ''' like a review but without a rating and transient '''
    book = fields.ForeignKey(
        'Edition', on_delete=models.PROTECT, activitypub_field='inReplyToBook')

    @property
    def pure_content(self):
        ''' indicate the book in question for mastodon (or w/e) users '''
        return '%s<p>(comment on <a href="%s">"%s"</a>)</p>' % \
                (self.content, self.book.remote_id, self.book.title)

    activity_serializer = activitypub.Comment
    pure_type = 'Note'


class Quotation(Status):
    ''' like a review but without a rating and transient '''
    quote = fields.HtmlField()
    book = fields.ForeignKey(
        'Edition', on_delete=models.PROTECT, activitypub_field='inReplyToBook')

    @property
    def pure_content(self):
        ''' indicate the book in question for mastodon (or w/e) users '''
        quote = re.sub(r'^<p>', '<p>"', self.quote)
        quote = re.sub(r'</p>$', '"</p>', quote)
        return '%s <p>-- <a href="%s">"%s"</a></p>%s' % (
            quote,
            self.book.remote_id,
            self.book.title,
            self.content,
        )

    activity_serializer = activitypub.Quotation
    pure_type = 'Note'


class Review(Status):
    ''' a book review '''
    name = fields.CharField(max_length=255, null=True)
    book = fields.ForeignKey(
        'Edition', on_delete=models.PROTECT, activitypub_field='inReplyToBook')
    rating = fields.IntegerField(
        default=None,
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )

    @property
    def pure_name(self):
        ''' clarify review names for mastodon serialization '''
        if self.rating:
            #pylint: disable=bad-string-format-type
            return 'Review of "%s" (%d stars): %s' % (
                self.book.title,
                self.rating,
                self.name
            )
        return 'Review of "%s": %s' % (
            self.book.title,
            self.name
        )

    @property
    def pure_content(self):
        ''' indicate the book in question for mastodon (or w/e) users '''
        return self.content

    activity_serializer = activitypub.Review
    pure_type = 'Article'


class Boost(ActivityMixin, Status):
    ''' boost'ing a post '''
    boosted_status = fields.ForeignKey(
        'Status',
        on_delete=models.PROTECT,
        related_name='boosters',
        activitypub_field='object',
    )
    activity_serializer = activitypub.Boost

    def save(self, *args, **kwargs):
        ''' save and notify '''
        super().save(*args, **kwargs)
        if not self.boosted_status.user.local:
            return

        notification_model = apps.get_model(
            'bookwyrm.Notification', require_ready=True)
        notification_model.objects.create(
            user=self.boosted_status.user,
            related_status=self.boosted_status,
            related_user=self.user,
            notification_type='BOOST',
        )

    def delete(self, *args, **kwargs):
        ''' delete and un-notify '''
        notification_model = apps.get_model(
            'bookwyrm.Notification', require_ready=True)
        notification_model.objects.filter(
            user=self.boosted_status.user,
            related_status=self.boosted_status,
            related_user=self.user,
            notification_type='BOOST',
        ).delete()
        super().delete(*args, **kwargs)


    def __init__(self, *args, **kwargs):
        ''' the user field is "actor" here instead of "attributedTo" '''
        super().__init__(*args, **kwargs)

        reserve_fields = ['user', 'boosted_status']
        self.simple_fields = [f for f in self.simple_fields if \
                f.name in reserve_fields]
        self.activity_fields = self.simple_fields
        self.many_to_many_fields = []
        self.image_fields = []
        self.deserialize_reverse_fields = []

    # This constraint can't work as it would cross tables.
    # class Meta:
    #     unique_together = ('user', 'boosted_status')
