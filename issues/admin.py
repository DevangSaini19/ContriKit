from django.contrib import admin
from .models import Tag, Issue, SavedIssue, SolvedIssue

@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'color')
    prepopulated_fields = {'slug': ('name',)}

@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ('title', 'repo', 'difficulty', 'estimated_hours', 'status', 'is_featured', 'view_count', 'closed_at')
    list_filter = ('difficulty', 'status', 'is_featured', 'tags')
    search_fields = ('title', 'description')
    readonly_fields = ('closed_at',)

@admin.register(SavedIssue)
class SavedIssueAdmin(admin.ModelAdmin):
    list_display = ('user', 'issue', 'saved_at')

@admin.register(SolvedIssue)
class SolvedIssueAdmin(admin.ModelAdmin):
    list_display = ('user', 'issue', 'is_verified', 'solved_at', 'github_pr_url')
    list_filter = ('is_verified', 'solved_at')
    search_fields = ('user__username', 'issue__title')
    readonly_fields = ('solved_at',)
