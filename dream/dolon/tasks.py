from __future__ import absolute_import

import logging
logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel('DEBUG')


from BeautifulSoup import BeautifulSoup


from django.core.files import File
import tempfile
import urllib2
import os

from dolon.models import *

from celery import shared_task, group

@shared_task
def search(qstring, start, end, manager, params):
    """
    Perform a search for ``string`` using a provided ``manager`` instance.
    
    Parameters
    ----------
    qstring : str
        Search query.
    start : int
        Start index for results.
    end : int
        End index for results.
    manager : :class:`.BaseSearchManager`
    params : list
        A list of parameters to pass to the remote search service.
    
    Returns
    -------
    result : dict
        Contains structured search results amenable to :class:`.QueryItem`
    response : dict
        Full parsed JSON response.
    """
    
    result, response = manager.imageSearch(params, qstring, start=start)
    
    return result, response
    
@shared_task
def processSearch(searchresult, queryeventid):
    """
    Create a :class:`.QueryResult` and a set of :class:`.QueryItem` from a
    search result.
    
    Parameters
    ----------
    searchresult : tuple
        ( result(dict), response(dict) ) from :func:`.search`
    
    Returns
    -------
    queryResult : :class:`.QueryResult`
    queryItems : list
        A list of :class:`.QueryItem` instances.
    """

    
    result, response = searchresult
    
    print result

    queryResult = QueryResult(  rangeStart=result['start'],
                                rangeEnd=result['end'],
                                result=response )
    queryResult.save()

    queryItems = []
    for item in result['items']:
        # Should only be one QueryItem per URI.
        queryItem = QueryItem.objects.get_or_create(
                        url = item['url'],
                        defaults = {
                            'title': item['title'],
                            'size': item['size'],
                            'height': item['height'],
                            'width': item['width'],
                            'mime': item['mime'],
                            'contextURL': item['contextURL'],
                            'thumbnailURL': item['thumbnailURL']
                        }   )[0]
        queryItem.save()

        queryResult.items.add(queryItem)
        queryItems.append(queryItem)

    queryResult.save()
    queryevent = QueryEvent.objects.get(id=queryeventid)
    queryevent.queryresults.add(queryResult)
    queryevent.save()

    return queryResult, queryItems   
    
@shared_task
def spawnThumbnails(processresult, queryeventid):
    """
    Dispatch tasks to retrieve and store thumbnails. Updates the corresponding
    :class:`.QueryEvent` `thumbnail_task` property with task id.
    
    Parameters
    ----------
    processresult : tuple
        ( :class:`.QueryResult` , queritems(list) ) from :func:`.processSearch`
    """
    
    queryresult, queryitems = processresult
    
    logger.debug('spawnThumbnails: creating jobs')    
    job = group( ( getFile.s(item.url) 
                    | storeThumbnail.s(item.id) 
                    ) for item in queryitems )

    logger.debug('spawnThumbnails: dispatching jobs')
    result = job.apply_async()    
    
    logger.debug('spawnThumbnails: jobs dispatched')

    task = Task(task_id=result.id)
    task.save()
    
    logger.debug('created new Task object')

    queryevent = QueryEvent.objects.get(id=queryeventid)
    queryevent.thumbnail_tasks.add(task)
    queryevent.save()
    
    logger.debug('updated QueryEvent')

    return task    

@shared_task
def getFile(url):
    """
    Retrieve a resource from `URL`.
    
    Parameters
    ----------
    url : str
        Resource location.
    
    Returns
    -------
    url : str
    filename : str
        Best guess at the resource's local filename.
    fpath : str
        Path to a temporary file containing retrieved data.
    mime : str
        MIME type.
    size : int
        Filesize.
    """

    filename = url.split('/')[-1]
    response = urllib2.urlopen(url)
    
    mime = dict(response.info())['content-type']
    size = int(dict(response.info())['content-length'])
    
    f_,fpath = tempfile.mkstemp()
    with open(fpath, 'w') as f:
        f.write(response.read())

    return url, filename, fpath, mime, size
    
@shared_task
def storeThumbnail(result, itemid):
    """
    Create a new :class:`.Thumbnail` and attach it to an :class:`.Item`\.
    
    Parameters
    ----------
    result : tuple
        ( url, filename, fpath, mime, size ) from :func:`.getFile`
    itemid : int
        ID of a :class:`.Item` instance associated with the :class:`.Thumbnail`
    
    Returns
    -------
    thumbnail.id : int
        ID for the :class:`.Thumbnail`
    """
    
    url, filename, fpath, mime, size = result

    thumbnail = Thumbnail(  url = url,
                            mime = mime,
                            size = size )
    
    with open(fpath, 'rb') as f:
        file = File(f)
        thumbnail.image.save(filename, file, True)
        thumbnail.save()

    os.remove(fpath)

    item = Item.objects.get(id=itemid)
    item.thumbnail = thumbnail
    item.save()

    return thumbnail.id
    
@shared_task
def storeImage(result, itemid):
    """
    Create a new :class:`.Image` and attach it to an :class:`.Item`\.
    
    Parameters
    ----------
    result : tuple
        ( url, filename, fpath, mime, size ) from :func:`.getFile`
    itemid : int
        ID of a :class:`.Item` instance associated with the :class:`.Image`
    
    Returns
    -------
    image.id : int
        ID for the class:`.Image`
    """
    
    url, filename, fpath, mime, size = result
    
    image = Image(  url = url,
                    mime = mime,
                    size = size )

    with open(fpath, 'rb') as f:
        file = File(f)
        image.image.save(filename, file, True)
        image.save()

    item = Item.objects.get(id=itemid)
    item.image = image
    item.save()
        
    return image.id
    
@shared_task
def getStoreContext(url, itemid):
    """
    Retrieve the HTML contents of a resource and attach it to an :class:`.Item`
    
    Parameters
    ----------
    url : str
        Location of resource.
        
    Returns
    -------
    context.id : int
        ID for the :class:`.Context`
    """

    response = urllib2.urlopen(url).read()
    soup = BeautifulSoup(response)
#    text = p.html
    title = soup.title.getText()

    context = Context(  url = url,
                        title = title,
                        content = response  )
    context.save()
    
    item = Item.objects.get(id=itemid)
    item.context = context
    item.save()    
    
    return context.id

