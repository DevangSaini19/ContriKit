from django.urls import path
from . import views

urlpatterns = [
    path('', views.issue_list_view, name='issue_list'),
    path('recommended/', views.recommended_issues_view, name='recommended_issues'),
    path('<int:id>/', views.issue_detail_view, name='issue_detail'),
    path('<int:id>/toggle-save/', views.toggle_save_view, name='toggle_save'),
    path('<int:id>/mark-solved/', views.mark_solved_view, name='mark_solved'),
    path('<int:id>/verify-contribution/', views.verify_contribution_view, name='verify_contribution'),
]
